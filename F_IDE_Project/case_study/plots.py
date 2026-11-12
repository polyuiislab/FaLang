from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Sequence

from case_study.simulation import ScheduledOperation, SimulationMetrics
from case_study.statistics import METRIC_NAMES, grouped_values


COLORS = {
    "falang": "#2f6f4e",
    "no_mfr": "#4c78a8",
    "no_pbqp": "#f58518",
    "no_try_catch": "#e45756",
    "fcfs": "#9b5a44",
    "edd": "#4c78a8",
    "atc": "#b279a2",
    "ga": "#f58518",
    "jmedd": "#54a24b",
    "jit": "#e45756",
}

DEFAULT_METHODS = ["falang", "fcfs", "edd", "atc", "ga", "jmedd", "jit"]


def _svg(path: Path, width: int, height: int, body: str) -> None:
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        f'<rect width="100%" height="100%" fill="white"/>\n{body}\n</svg>\n',
        encoding="utf-8",
    )


def metric_box_plots(rows: Sequence[SimulationMetrics], out_dir: Path, methods: Sequence[str] | None = None) -> None:
    """Draw medians, interquartile ranges, 1.5-IQR whiskers, and individual observations."""
    from case_study.statistics import percentile

    methods = list(methods or DEFAULT_METHODS)

    for metric in METRIC_NAMES:
        groups = grouped_values(rows, metric)

        box_stats: Dict[str, dict] = {}
        for method in methods:
            values = sorted(groups.get(method, []))
            if not values:
                continue
            q1 = percentile(values, 0.25)
            q2 = percentile(values, 0.50)
            q3 = percentile(values, 0.75)
            iqr = q3 - q1
            if iqr < 1e-9:
                lower_whisker = values[0]
                upper_whisker = values[-1]
                outliers: List[float] = []
            else:
                lower_fence = q1 - 1.5 * iqr
                upper_fence = q3 + 1.5 * iqr
                lower_whisker = next((v for v in values if v >= lower_fence), values[0])
                upper_whisker = next((v for v in reversed(values) if v <= upper_fence), values[-1])
                outliers = [v for v in values if v < lower_whisker or v > upper_whisker]
            box_stats[method] = {
                "q1": q1,
                "median": q2,
                "q3": q3,
                "lower_whisker": lower_whisker,
                "upper_whisker": upper_whisker,
                "outliers": outliers,
                "values": list(values),
            }

        width, height = 640, 480
        left, right = 80, 610
        top_margin, bottom = 58, 390
        chart_w = right - left
        chart_h = bottom - top_margin

        all_vals = [v for vals in groups.values() for v in vals]
        y_min = min(all_vals) if all_vals else 0.0
        y_max = max(all_vals) if all_vals else 1.0
        y_range = y_max - y_min
        y_pad = y_range * 0.08 if y_range > 0 else 0.1
        y_lo = y_min - y_pad
        y_hi = y_max + y_pad

        def _y(val: float) -> float:
            return bottom - (val - y_lo) / max(y_hi - y_lo, 1e-9) * chart_h

        n_methods = len(methods)
        slot_w = chart_w / n_methods
        box_w = slot_w * 0.643
        jitter_w = box_w * 0.70

        parts: List[str] = []

        title_map = {
            "mof": "MOFT",
            "tst": "TST",
            "on_time_delivery_rate": "OTDR",
            "average_wip": "Mean WIP",
        }
        title = title_map.get(metric, metric.replace("_", " ").title())
        parts.append(
            f'<text x="{left + chart_w / 2:.1f}" y="48" text-anchor="middle" '
            f'font-size="17" font-family="Times New Roman, serif">{title}</text>'
        )

        parts.append(
            f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" '
            f'stroke="#333" stroke-width="0.8"/>'
        )
        parts.append(
            f'<line x1="{left}" y1="{top_margin}" x2="{left}" y2="{bottom}" '
            f'stroke="#333" stroke-width="0.8"/>'
        )
        parts.append(
            f'<line x1="{left}" y1="{top_margin}" x2="{right}" y2="{top_margin}" '
            f'stroke="#333" stroke-width="0.8"/>'
        )
        parts.append(
            f'<line x1="{right}" y1="{top_margin}" x2="{right}" y2="{bottom}" '
            f'stroke="#333" stroke-width="0.8"/>'
        )

        n_ticks = 6
        for i in range(n_ticks + 1):
            frac = i / n_ticks
            val = y_lo + frac * (y_hi - y_lo)
            y = _y(val)
            parts.append(
                f'<line x1="{left - 5}" y1="{y:.1f}" x2="{left}" y2="{y:.1f}" '
                f'stroke="#333" stroke-width="0.8"/>'
            )
            parts.append(
                f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" '
                f'stroke="#e0e0e0" stroke-width="0.4"/>'
            )
            parts.append(
                f'<text x="{left - 8}" y="{y + 6:.1f}" text-anchor="end" '
                f'font-size="17" font-family="Times New Roman, serif" fill="#222">{val:.4g}</text>'
            )

        for i, method in enumerate(methods):
            stats = box_stats.get(method)
            if not stats:
                continue

            x_center = left + (i + 0.5) * slot_w
            color = COLORS[method]
            x_l = x_center - box_w / 2
            x_r = x_center + box_w / 2
            cap = box_w * 0.30

            uw_y = _y(stats["upper_whisker"])
            q3_y = _y(stats["q3"])
            parts.append(
                f'<line x1="{x_center:.1f}" y1="{q3_y:.1f}" '
                f'x2="{x_center:.1f}" y2="{uw_y:.1f}" '
                f'stroke="#555" stroke-width="1.0"/>'
            )
            parts.append(
                f'<line x1="{x_center - cap:.1f}" y1="{uw_y:.1f}" '
                f'x2="{x_center + cap:.1f}" y2="{uw_y:.1f}" '
                f'stroke="#555" stroke-width="1.0"/>'
            )

            lw_y = _y(stats["lower_whisker"])
            q1_y = _y(stats["q1"])
            parts.append(
                f'<line x1="{x_center:.1f}" y1="{q1_y:.1f}" '
                f'x2="{x_center:.1f}" y2="{lw_y:.1f}" '
                f'stroke="#555" stroke-width="1.0"/>'
            )
            parts.append(
                f'<line x1="{x_center - cap:.1f}" y1="{lw_y:.1f}" '
                f'x2="{x_center + cap:.1f}" y2="{lw_y:.1f}" '
                f'stroke="#555" stroke-width="1.0"/>'
            )

            box_top = _y(stats["q3"])
            box_h = _y(stats["q1"]) - box_top
            parts.append(
                f'<rect x="{x_l:.1f}" y="{box_top:.1f}" width="{box_w:.1f}" '
                f'height="{max(box_h, 0.5):.1f}" fill="{color}" fill-opacity="0.22" '
                f'stroke="{color}" stroke-width="1.4"/>'
            )

            med_y = _y(stats["median"])
            parts.append(
                f'<line x1="{x_l:.1f}" y1="{med_y:.1f}" x2="{x_r:.1f}" y2="{med_y:.1f}" '
                f'stroke="#222" stroke-width="1.8"/>'
            )

            for vi, val in enumerate(stats["values"]):
                # Use deterministic jitter so repeated plots retain the same point positions.
                jseed = (vi * 2654435761) & 0xFFFFFFFF
                jitter = ((jseed / 0xFFFFFFFF) - 0.5) * jitter_w
                py = _y(val)
                parts.append(
                    f'<circle cx="{x_center + jitter:.1f}" cy="{py:.1f}" r="1.8" '
                    f'fill="{color}" fill-opacity="0.32"/>'
                )

            for out_val in stats["outliers"]:
                oy = _y(out_val)
                parts.append(
                    f'<circle cx="{x_center:.1f}" cy="{oy:.1f}" r="3.2" '
                    f'fill="none" stroke="{color}" stroke-width="1.2"/>'
                )

            parts.append(
                f'<text x="{x_center:.1f}" y="{bottom + 21}" text-anchor="middle" '
                f'font-size="17" font-family="Times New Roman, serif" fill="#222">{method.upper()}</text>'
            )

        _svg(out_dir / f"metric_{metric}.svg", width, height, "\n".join(parts))


def wip_curve(schedules: Sequence[ScheduledOperation], out_dir: Path, method: str, seed: int) -> None:
    by_order: Dict[int, List[ScheduledOperation]] = defaultdict(list)
    for item in schedules:
        by_order[item.order_id].append(item)
    events = []
    for ops in by_order.values():
        start = min(op.ready_time for op in ops)
        finish = max(op.finish for op in ops)
        events.append((start, 1))
        events.append((finish, -1))
    events.sort()
    if not events:
        return
    t_min, t_max = events[0][0], events[-1][0]
    width, height = 820, 360
    left, bottom = 60, 310
    chart_w, chart_h = 710, 250
    max_wip = 1
    current = 0
    points = []
    for t, delta in events:
        points.append((t, current))
        current += delta
        points.append((t, current))
        max_wip = max(max_wip, current)
    coords = []
    for t, wip in points:
        x = left + (t - t_min) / max(t_max - t_min, 1.0) * chart_w
        y = bottom - wip / max_wip * chart_h
        coords.append(f"{x:.1f},{y:.1f}")
    body = [
        f'<text x="{left}" y="34" font-size="20" font-family="Arial">WIP over time: {method}, seed={seed}</text>',
        f'<line x1="{left}" y1="{bottom}" x2="{left + chart_w}" y2="{bottom}" stroke="#333"/>',
        f'<line x1="{left}" y1="{bottom - chart_h}" x2="{left}" y2="{bottom}" stroke="#333"/>',
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="{COLORS.get(method, "#333")}" stroke-width="2"/>',
        f'<text x="{left - 8}" y="{bottom - chart_h}" text-anchor="end" font-size="11" font-family="Arial">{max_wip}</text>',
        f'<text x="{left + chart_w}" y="{bottom + 24}" text-anchor="end" font-size="11" font-family="Arial">{t_max:.1f}</text>',
    ]
    _svg(out_dir / f"wip_{method}_seed_{seed}.svg", width, height, "\n".join(body))


def utilization_heatmap(schedules: Sequence[ScheduledOperation], out_dir: Path, method: str, seed: int) -> None:
    busy: Dict[int, float] = defaultdict(float)
    for op in schedules:
        busy[op.workstation_id] += op.processing_time + op.setup_time
    horizon = max((op.finish for op in schedules), default=1.0) - min((op.ready_time for op in schedules), default=0.0)
    horizon = max(1.0, horizon)
    width, height = 440, 470
    cell = 82
    left, top = 55, 60
    parts = [f'<text x="{left}" y="34" font-size="18" font-family="Arial">Resource utilization: {method}, seed={seed}</text>']
    for wid in range(16):
        r, c = divmod(wid, 4)
        util = min(1.0, busy.get(wid, 0.0) / horizon)
        shade = int(245 - 160 * util)
        color = f"rgb({shade},{245 - int(75 * util)},{shade})"
        x, y = left + c * cell, top + r * cell
        parts.append(f'<rect x="{x}" y="{y}" width="{cell - 8}" height="{cell - 8}" fill="{color}" stroke="#666"/>')
        parts.append(f'<text x="{x + 8}" y="{y + 22}" font-size="13" font-family="Arial">PM {wid}</text>')
        parts.append(f'<text x="{x + 8}" y="{y + 48}" font-size="13" font-family="Arial">{util:.2f}</text>')
    _svg(out_dir / f"utilization_{method}_seed_{seed}.svg", width, height, "\n".join(parts))


def gantt_chart(schedules: Sequence[ScheduledOperation], out_dir: Path, method: str, seed: int, limit: int = 140) -> None:
    ops = sorted(schedules, key=lambda item: item.start)[:limit]
    if not ops:
        return
    t_min, t_max = min(op.ready_time for op in ops), max(op.finish for op in ops)
    width, height = 960, 520
    left, top = 80, 50
    chart_w, chart_h = 820, 410
    row_h = chart_h / 16
    parts = [
        f'<text x="{left}" y="30" font-size="18" font-family="Arial">Gantt example: {method}, seed={seed}</text>',
        f'<line x1="{left}" y1="{top + chart_h}" x2="{left + chart_w}" y2="{top + chart_h}" stroke="#333"/>',
    ]
    for wid in range(16):
        y = top + wid * row_h
        parts.append(f'<text x="{left - 8}" y="{y + row_h * 0.7:.1f}" text-anchor="end" font-size="10" font-family="Arial">PM {wid}</text>')
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + chart_w}" y2="{y:.1f}" stroke="#eee"/>')
    for op in ops:
        x = left + (op.start - t_min) / max(t_max - t_min, 1.0) * chart_w
        w = max(1.0, (op.finish - op.start) / max(t_max - t_min, 1.0) * chart_w)
        y = top + op.workstation_id * row_h + 3
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{row_h - 6:.1f}" fill="{COLORS.get(method, "#888")}" opacity="0.8"/>')
    _svg(out_dir / f"gantt_{method}_seed_{seed}.svg", width, height, "\n".join(parts))


def write_all_plots(
    rows: Sequence[SimulationMetrics],
    schedules_by_key: Dict[tuple[str, int], List[ScheduledOperation]],
    out_dir: Path,
    methods: Sequence[str] | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    methods = list(methods or DEFAULT_METHODS)
    metric_box_plots(rows, out_dir, methods)
    for method in methods:
        candidates = [key for key in schedules_by_key if key[0] == method]
        if not candidates:
            continue
        key = sorted(candidates, key=lambda item: item[1])[0]
        schedules = schedules_by_key[key]
        wip_curve(schedules, out_dir, key[0], key[1])
        utilization_heatmap(schedules, out_dir, key[0], key[1])
        gantt_chart(schedules, out_dir, key[0], key[1])
