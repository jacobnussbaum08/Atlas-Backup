"""Register historically verified lot sizes for 2019-2020 (E2/E3 stage-A deviation, D-238).

Simulation database only. Reads runs/lot_size_evidence.json (lot_size_evidence.py) and, for
every VERIFIED symbol, appends one instrument specification (quantity increment 1, minimum 1,
no price increment: the same fields as the existing 2021 records) valid from the first
sampled session of its evidence. NOT_VERIFIED symbols get nothing. Existing records are never
changed (``instrument_specs`` is keyed by asset and valid_from; the 2021-01-01 records keep
governing from 2021). Runs created before this registration cannot see the new rows: the
backtest loads specifications created by its knowledge time.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

from analyze import MANIFEST
from sqlalchemy import select

from app.accounting.enums import InstrumentSpecSource
from app.accounting.reference import register_instrument_spec
from app.core.config import get_settings
from app.database.models import Asset, InstrumentSpecRecord
from app.database.sessions import build_engine, build_session_factory

EVIDENCE = MANIFEST.parent / "lot_size_evidence.json"


def main() -> None:
    raw = EVIDENCE.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    evidence = json.loads(raw)
    factory = build_session_factory(build_engine(get_settings()))
    added, skipped = [], []
    with factory() as session, session.begin():
        for symbol, info in sorted(evidence["symbols"].items()):
            if info["status"] != "VERIFIED":
                skipped.append((symbol, info["status"]))
                continue
            asset = session.scalar(select(Asset).where(Asset.symbol == symbol))
            first = date.fromisoformat(info["first_sampled_session"])
            valid_from = datetime(first.year, first.month, first.day, tzinfo=UTC)
            exists = session.scalar(
                select(InstrumentSpecRecord.id).where(
                    InstrumentSpecRecord.asset_id == asset.id,
                    InstrumentSpecRecord.valid_from == valid_from,
                )
            )
            if exists:
                skipped.append((symbol, "already registered"))
                continue
            last = max(info["by_session"])
            register_instrument_spec(
                session,
                asset_id=asset.id,
                quantity_increment=Decimal(1),
                min_quantity=Decimal(1),
                valid_from=valid_from,
                source=InstrumentSpecSource.OPERATOR,
                provenance=(
                    "D-238 historical lot-size repair (E2/E3 stage-A deviation): whole-share "
                    f"trading verified from SIP trade prints, {info['sessions']} sampled sessions "
                    f"{first.isoformat()}..{last} (>=1 one-share print each, "
                    f"{info['non_integer_prints']} non-integer sizes, {info['prints']} prints); "
                    f"evidence runs/lot_size_evidence.json sha256 {sha}"
                ),
                created_by="operator:e2e3-stage-a-deviation",
            )
            added.append((symbol, valid_from.date().isoformat()))
    print({"evidence_sha256": sha, "added": added, "skipped": skipped})


if __name__ == "__main__":
    main()
