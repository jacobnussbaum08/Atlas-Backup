"""Audit 8: hashes, recorded code and input hashes, X6 V2 use, and report-vs-artifact numbers."""
import gzip
import hashlib
import json
import re
from pathlib import Path


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


res = json.loads(gzip.decompress(Path("config/simulation/lifecycle/x7_backtest_results_v1.json.gz").read_bytes()))
sig = json.loads(gzip.decompress(Path("config/simulation/lifecycle/x7_signals_v1.json.gz").read_bytes()))
report = Path("docs/phase-reports/x7-backtest-integration-report.md").read_text(encoding="utf-8")
files = {
    "config/simulation/lifecycle/x7_signals_v1.json.gz": "36f114735d3946008d5b25fd06ead03c457c9aba9342c7c3ddc522e4887bdca1",
    "config/simulation/lifecycle/x7_backtest_results_v1.json.gz": "01eb9da3d8e88826005b5e7bc62ddb4110ee3b76f4a62cd6538e113abad6d6c2",
    "config/simulation/lifecycle/x6_execution_inputs_v2.json.gz": "917948267f925a73f443d138f97ff610fb4b9f2bee6f71e8e9b9e6eb8df83c33",
    "config/simulation/lifecycle/x6_execution_inputs_v1.json.gz": "aeda44c15f08d9ce964b087069c28ae9f219ff2dec1b5ad149bb7508326e4d39",
    "config/simulation/x7/registered_strategies_v2.json": "2331245f71a944c0fa49dd332d8cda2a42947b56205af1604b22831bc34894b7",
    "config/simulation/evidence/x7/sip_quoted_spreads_2017_2019.json.gz": "d380348428c7172d87c854f9a1f76d6526c8681cd873009d1c38cb583cbd079c",
    "config/simulation/lifecycle/x5_corporate_actions_v1.json.gz": "c320e96dc57f726231d7120075ddc743f0f852d7c45976639b917af0a2b58d94",
    "config/simulation/lifecycle/x5_total_returns_v1.json.gz": "eef8fb05d2d64d4d5e637e44cafb2361616d79fc5dcbaca94c848d675d310642",
}
for f, h in files.items():
    print("file", "OK " if sha(f) == h else "BAD", f)
print("report states every artifact hash:", all(h in report for h in list(files.values())[:3]))
print("results pins include X6 V2:", any("x6_execution_inputs_v2" in k for k in res["inputs"]["pins"]))
for p_, h in res["inputs"]["sources_sha256"].items():
    print("source", "same" if sha(p_) == h else "CHANGED SINCE BUILD", p_)
print("signals_content in results:", res["inputs"]["signals_content_sha256"] == sig["content_sha256"])
# report table vs artifact: parse "| set | 2017–19 | total | ... |" rows of the selected-results table
R = {(r["strategy_set"], r["period"], r["scenario"]): r for r in res["runs"]}
names = {"breakout": "breakout", "mean reversion": "mean_reversion", "momentum": "momentum", "trend following": "trend_following",
         "volatility": "volatility", "combined": "combined"}
pers = {"2017–19": "continuous_2017_2019", "2017–19 †": "continuous_2017_2019", "2017": "year_2017", "2018": "year_2018", "2019": "year_2019"}
bad = 0
checked = 0
for line in report.splitlines():
    m = re.match(r"\| (breakout|mean reversion|momentum|trend following|volatility|combined) \| (2017–19 †|2017–19|2017|2018|2019) \| ([−+\-\d.]+)% \|", line)
    if not m:
        continue
    r = R[(names[m.group(1)], pers[m.group(2)], "baseline")]
    rep = float(m.group(3).replace("−", "-").replace("+", ""))
    checked += 1
    if abs(rep - r["headline"]["total_return"] * 100) > 0.006:
        bad += 1
        print("REPORT MISMATCH", line[:80], r["headline"]["total_return"])
print("report total-return cells checked", checked, "mismatches", bad)
# sensitivity table (continuous) in the report
for line in report.splitlines():
    m = re.match(r"\| (mean reversion|trend following|breakout|volatility|combined) \| ([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|([^|]+)\|$", line)
    if not m:
        continue
    vals = [m.group(i).strip() for i in range(2, 9)]
    scen = ["baseline", "spread_x2", "slippage_x2", "impact_x2", "all_adverse", "x6_estimated_spread", "costs_off"]
    for v, sc in zip(vals, scen):
        x = float(v.replace("*", "").replace("%", "").replace("−", "-").replace("+", ""))
        rr = R[(names[m.group(1)], "continuous_2017_2019", sc)]
        if abs(x - rr["headline"]["total_return"] * 100) > 0.006:
            print("SENS MISMATCH", m.group(1), sc, v, rr["headline"]["total_return"])
        if ("*" in v) == rr["equity_complete"]:
            print("SENS INCOMPLETE MARK WRONG", m.group(1), sc, v, rr["equity_complete"])
print("sensitivity table checked")
