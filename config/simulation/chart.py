"""Draw the equity-curve figure for the profitability report from ``runs/results.json``
as a static SVG (no plotting library is installed). Reads stored results only."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
RESULTS = HERE / "runs" / "results.json"
OUT = HERE.parent.parent / "docs" / "phase-reports" / "profitability-equity-curves.svg"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

PANELS = [
    (
        "Main period 2022-01 to 2026-06, combined portfolio (one account; flat after halt)",
        [
            ("8A replay, IEX", "main--8a-iex--all6"),
            ("8A replay, SIP", "main--8a-sip--all6"),
            ("Live pipeline, IEX, assumed spread", "main--live-iex-assumed--all6"),
            ("Live pipeline, IEX, measured spread", "main--live-iex-measured--all6"),
            ("Live pipeline, SIP, measured spread", "main--live-sip-measured--all6"),
        ],
    ),
    (
        "Independent yearly windows, live pipeline, SIP, measured spread (fresh account each)",
        [
            ("2022", "y2022--live-sip-measured--all6"),
            ("2023", "y2023--live-sip-measured--all6"),
            ("2024", "y2024--live-sip-measured--all6"),
            ("2025", "y2025--live-sip-measured--all6"),
            ("2026 H1", "y2026h1--live-sip-measured--all6"),
        ],
    ),
]


def main() -> None:
    results = json.loads(RESULTS.read_text())
    width, panel_h, top, left, right = 960, 300, 40, 56, 260
    height = len(PANELS) * (panel_h + 70) + 10
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'font-family="system-ui, sans-serif" font-size="12">',
        f'<rect width="{width}" height="{height}" fill="{SURFACE}"/>',
    ]
    lo, hi = 0.0, 1.0  # REDACTED_FOR_GITHUB
    for p, (title, series) in enumerate(PANELS):
        y0 = 10 + p * (panel_h + 70)
        plot_top, plot_bot = y0 + top, y0 + top + panel_h - 40
        plot_l, plot_r = left, width - right
        parts.append(
            f'<text x="{plot_l}" y="{y0 + 20}" fill="{INK}" font-weight="600">{title}</text>'
        )

        def ys(v: float, pt: float = plot_top, pb: float = plot_bot) -> float:
            return pb - (v - lo) / (hi - lo) * (pb - pt)

        for v in range(int(lo), int(hi) + 1, 2):
            y = ys(v)
            parts.append(
                f'<line x1="{plot_l}" x2="{plot_r}" y1="{y:.1f}" y2="{y:.1f}" '
                f'stroke="{GRID}" stroke-width="{1.5 if v == 100 else 1}"/>'
                f'<text x="{plot_l - 8}" y="{y + 4:.1f}" fill="{INK2}" '
                f'text-anchor="end">{v}</text>'
            )
        curves = []
        for label, key in series:
            curve = results[key]["equity_curve"]
            capital = float(results[key]["initial_capital"])
            pts = [(date.fromisoformat(d), e / capital * 100) for d, e in curve]
            curves.append((label, pts))
        if p == 0:
            d0 = min(pts[0][0] for _, pts in curves)
            d1 = max(pts[-1][0] for _, pts in curves)
            span = (d1 - d0).days

            def xs(d: date, i: int) -> float:
                return plot_l + (d - d0).days / span * (plot_r - plot_l)  # noqa: B023

            for yr in range(2022, 2027):
                x = xs(max(date(yr, 1, 1), d0), 0)
                if plot_l <= x <= plot_r:
                    parts.append(
                        f'<text x="{x:.1f}" y="{plot_bot + 16}" fill="{INK2}" '
                        f'text-anchor="middle">{yr}</text>'
                    )
        else:

            def xs(d: date, i: int) -> float:  # sessions since the window start
                return plot_l + i / 251 * (plot_r - plot_l)  # noqa: B023

            for s in (0, 63, 126, 189, 251):
                x = plot_l + s / 251 * (plot_r - plot_l)
                parts.append(
                    f'<text x="{x:.1f}" y="{plot_bot + 16}" fill="{INK2}" '
                    f'text-anchor="middle">{s}</text>'
                )
            parts.append(
                f'<text x="{(plot_l + plot_r) / 2}" y="{plot_bot + 32}" fill="{INK2}" '
                'text-anchor="middle">sessions into the window</text>'
            )
        for n, (label, pts) in enumerate(curves):
            path = " ".join(
                f"{'M' if i == 0 else 'L'}{xs(d, i):.1f},{ys(v):.1f}"
                for i, (d, v) in enumerate(pts)
            )
            parts.append(
                f'<path d="{path}" fill="none" stroke="{SERIES[n]}" stroke-width="2" '
                f'stroke-linejoin="round"/>'
            )
            ly = plot_top + 14 + n * 32
            final = pts[-1][1] - 100
            parts.append(
                f'<line x1="{plot_r + 16}" x2="{plot_r + 34}" y1="{ly - 4}" y2="{ly - 4}" '
                f'stroke="{SERIES[n]}" stroke-width="3" stroke-linecap="round"/>'
                f'<text x="{plot_r + 40}" y="{ly}" fill="{INK}">{label}</text>'
                f'<text x="{plot_r + 40}" y="{ly + 12}" fill="{INK2}" font-size="11">'
                f"{final:+.1f}% net</text>"
            )
    parts.append(
        f'<text x="{left}" y="{height - 6}" fill="{INK2}" font-size="11">Equity indexed to '
        "100 = initial capital. Simulated on historical data; not live results."
        "</text></svg>"
    )
    OUT.write_text("\n".join(parts))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
