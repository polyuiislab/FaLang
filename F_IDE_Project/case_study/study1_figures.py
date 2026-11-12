"""Plot domain layouts, process routes, and placement results."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from case_study.domains import DOMAIN_KEYS, spec_of
from case_study.study1 import FT_STATES, Study1Record

DOMAIN_TITLES = {
    "pc_assembly": "Customized PC assembly",
    "garment": "Customized garment sewing",
    "cnc_machining": "Customized CNC machining",
}

PARADIGM_TITLES = {
    "matrix": "matrix-structured manufacturing",
    "flexible_assembly_line": "flexible assembly line",
    "flexible_job_shop": "flexible job shop",
}

STATE_TITLES = {
    "normal": "normal",
    "high_occupancy": "high occupancy",
    "machine_unavailable": "one machine down",
}

STATE_COLORS = {
    "normal": "#2f6f4e",
    "high_occupancy": "#c98a1b",
    "machine_unavailable": "#a63a2f",
}


def _load(path: Path) -> List[Study1Record]:
    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Study1Record(**row) for row in raw]


def render(records_path: Path, out_stem: Path) -> Dict[str, Path]:
    records = _load(records_path)
    by_domain: Dict[str, Dict[str, Study1Record]] = {}
    for record in records:
        by_domain.setdefault(record.domain, {})[record.ft_state] = record

    figure, axes = plt.subplots(
        nrows=len(DOMAIN_KEYS), ncols=2,
        figsize=(13.0, 3.0 * len(DOMAIN_KEYS)),
        gridspec_kw={"width_ratios": [2.05, 1.0]},
    )

    for row, domain in enumerate(DOMAIN_KEYS):
        spec = spec_of(domain)
        per_state = by_domain.get(domain, {})
        _draw_route(axes[row][0], spec, domain)
        _draw_success(axes[row][1], per_state, domain)

    figure.suptitle(
        "One representation, one compiler core, three manufacturing domains",
        fontsize=13,
    )
    figure.tight_layout(rect=(0, 0, 1, 0.96), h_pad=3.0, w_pad=3.0)

    out_stem.parent.mkdir(parents=True, exist_ok=True)
    paths = {
        "png": out_stem.with_suffix(".png"),
        "svg": out_stem.with_suffix(".svg"),
    }
    figure.savefig(paths["png"], dpi=200, bbox_inches="tight")
    figure.savefig(paths["svg"], bbox_inches="tight")
    plt.close(figure)
    return paths


ROLE_COLORS = {
    "Recovery_PM": "#8a6d3b",
}


def _draw_route(axis, spec, domain: str) -> None:
    axis.set_title(
        f"{DOMAIN_TITLES.get(domain, domain)} — {PARADIGM_TITLES.get(spec.paradigm, spec.paradigm)}"
        f"  ({len(spec.workstations)} stations, {len(spec.register_ids)} holding positions)",
        fontsize=10, loc="left",
    )

    per_row = 6
    box_w, box_h = 1.55, 0.62
    gap = 0.42
    rows = (len(spec.rules) + per_row - 1) // per_row
    for index, rule in enumerate(spec.rules):
        row, col = divmod(index, per_row)
        x = col * (box_w + gap)
        y = -(row * (box_h + 0.72))
        role = rule.role or "Warehouse"
        color = ROLE_COLORS.get(role, "#2f6f4e")
        axis.add_patch(plt.Rectangle((x, y), box_w, box_h,
                                     facecolor=color, alpha=0.12,
                                     edgecolor=color, linewidth=0.9))
        axis.text(x + box_w / 2, y + box_h * 0.62, rule.name, ha="center", va="center",
                  fontsize=7.2)
        axis.text(x + box_w / 2, y + box_h * 0.24, f"<{role}>", ha="center", va="center",
                  fontsize=6.2, color="#3b4a44")
        if col < per_row - 1 and index < len(spec.rules) - 1:
            axis.annotate("", xy=(x + box_w + gap - 0.06, y + box_h / 2),
                          xytext=(x + box_w + 0.06, y + box_h / 2),
                          arrowprops={"arrowstyle": "->", "linewidth": 0.7, "color": "#5a6b64"})

    axis.set_xlim(-0.15, per_row * (box_w + gap))
    axis.set_ylim(-(rows - 1) * (box_h + 0.72) - 0.25, box_h + 0.35)
    axis.axis("off")


def _draw_success(axis, per_state: Mapping[str, Study1Record], domain: str) -> None:
    axis.set_title("Placement of the unchanged order programs",
                   fontsize=10, loc="left")
    states = [state for state in FT_STATES if state in per_state]
    values = [per_state[state].immediate_placement * 100.0 for state in states]
    labels = [STATE_TITLES.get(state, state) for state in states]
    colors = [STATE_COLORS.get(state, "#4c78a8") for state in states]

    bars = axis.bar(labels, values, color=colors, width=0.55)
    axis.set_ylim(0, 116)
    axis.set_ylabel("placed at entry  (%)", fontsize=8)
    axis.tick_params(labelsize=8)
    for bar, value in zip(bars, values):
        axis.text(bar.get_x() + bar.get_width() / 2, value + 2.0, f"{value:.1f}",
                  ha="center", fontsize=8)

    realized = [per_state[state].realization_success * 100.0 for state in states]
    axis.axhline(100.0, color="#2f6f4e", linewidth=1.0, linestyle="--")
    axis.text(-0.45, 110.0,
              f"realized after WAIT: {min(realized):.0f}%",
              ha="left", fontsize=7.5, color="#2f6f4e")
    axis.grid(axis="y", linestyle=":", linewidth=0.6, alpha=0.5)
    axis.set_axisbelow(True)
    for spine in ("top", "right"):
        axis.spines[spine].set_visible(False)


if __name__ == "__main__":
    from case_study.study1 import RESULTS_DIR

    outputs = render(RESULTS_DIR / "study1_records.json", RESULTS_DIR / "study1_realization")
    for key, path in outputs.items():
        print(f"{key}: {path}")
