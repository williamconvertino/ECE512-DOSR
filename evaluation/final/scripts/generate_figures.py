import csv
import statistics
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns

from common import FIGURES, MODEL_LABELS, RAW, RESULTS, load_config, read_jsonl

SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
MODEL_COLORS = {
    "openai/gpt-6-luna@latest": "#3a8ee6", "openai/gpt-6.2-sol@latest": "#5b3aa8",
    "anthropic/claude-haiku-5-5@latest": "#f29b38", "anthropic/claude-sonnet-5-5@latest": "#b8322f",
}
MODEL_ORDER = ["openai/gpt-6-luna@latest", "openai/gpt-6.2-sol@latest",
               "anthropic/claude-haiku-5-5@latest", "anthropic/claude-sonnet-5-5@latest"]
MARKERS = {"openai/gpt-6-luna@latest": "o", "anthropic/claude-haiku-5-5@latest": "s",
           "openai/gpt-6.2-sol@latest": "o", "anthropic/claude-sonnet-5-5@latest": "s"}
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
COL_W = 3.33


def style():
    sns.set_theme(style="whitegrid", context="paper")
    plt.rcParams.update({
        "font.family": "sans-serif", "font.size": 8, "axes.labelsize": 8, "xtick.labelsize": 7,
        "ytick.labelsize": 7, "legend.fontsize": 7, "pdf.fonttype": 42, "ps.fonttype": 42,
        "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
        "axes.edgecolor": INK2, "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK2,
        "grid.color": GRID, "grid.linewidth": 0.6, "axes.linewidth": 0.6,
        "axes.spines.top": False, "axes.spines.right": False,
    })


def save(fig, name):
    FIGURES.mkdir(exist_ok=True)
    import time

    for ext, kw in (("pdf", {}), ("png", {"dpi": 300})):
        for attempt in range(5):
            try:
                fig.savefig(FIGURES / f"{name}.{ext}", bbox_inches="tight", **kw)
                break
            except OSError:
                time.sleep(1)
        else:
            print(f"warning: could not write {name}.{ext} (is it open in another program?)")
    plt.close(fig)


def per_pr(runs, model, field, agg):
    by = defaultdict(list)
    for r in runs:
        if r["model"] == model and r.get(field) is not None:
            by[r["pr_id"]].append(r)
    xs, ys = [], []
    for rs in by.values():
        xs.append((rs[0]["lines_added"] or 0) + (rs[0]["lines_deleted"] or 0))
        ys.append(agg([r[field] for r in rs]))
    order = np.argsort(xs)
    return np.array(xs)[order], np.array(ys)[order]


def scatter(runs, models, field, agg, scale, ylabel, name, trend=False, logy=False):
    fig, ax = plt.subplots(figsize=(COL_W, 2.3))
    for m in models:
        x, y = per_pr(runs, m, field, agg)
        if not len(x):
            continue
        y = y * scale
        ax.scatter(x, y, s=18, color=MODEL_COLORS[m], marker=MARKERS[m], edgecolor="#3a3a38", linewidth=0.4,
                   alpha=0.9, label=MODEL_LABELS[m], zorder=3)
        if trend and len(x) > 2:
            b, a = np.polyfit(x, y, 1)
            xx = np.linspace(0, x.max(), 50)
            ax.plot(xx, a + b * xx, color=MODEL_COLORS[m], linewidth=1.2, alpha=0.8, zorder=2)
    ax.set_xlabel("Changed lines")
    ax.set_ylabel(ylabel)
    ax.set_xlim(left=0)
    if logy:
        from matplotlib.ticker import FuncFormatter

        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    else:
        ax.set_ylim(bottom=0)
    ax.legend(frameon=False, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=2, handletextpad=0.3,
              columnspacing=1.2)
    save(fig, name)


def breakdown():
    p = RESULTS / "pipeline_summary.csv"
    if not p.exists():
        return
    rows = list(csv.DictReader(p.open(newline="")))
    groups = [
        ("Client preparation", ["median_client_prep_ms"]),
        ("Attestor processing", ["median_attestor_validation_ms", "median_parse_sign_verify_ms"]),
        ("Model inference", ["median_provider_inference_ms"]),
        ("IPFS publication", ["median_ipfs_publish_ms"]),
    ]
    models = list(dict.fromkeys(r["model"] for r in rows))
    sizes = ["small", "medium", "large"]
    fig, axes = plt.subplots(1, len(models), figsize=(COL_W * 2, 2.2), sharey=True)
    for ax, m in zip(np.atleast_1d(axes), models):
        bottom = np.zeros(len(sizes))
        for i, (label, cols) in enumerate(groups):
            vals = []
            for s in sizes:
                r = next(r for r in rows if r["model"] == m and r["pipeline_size"] == s)
                vals.append(sum(float(r[c]) for c in cols) / 1000 if r["approved_runs"] != "0" and r[cols[0]] else 0)
            ax.bar([s.capitalize() for s in sizes], vals, 0.6, bottom=bottom, color=SLOTS[i], edgecolor="white",
                   linewidth=1.0, label=label, zorder=3)
            bottom += np.array(vals)
        ax.set_xlabel(MODEL_LABELS[m])
        ax.grid(axis="x", visible=False)
    np.atleast_1d(axes)[0].set_ylabel("Latency (s)")
    handles, labels = np.atleast_1d(axes)[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=4, frameon=False, bbox_to_anchor=(0.5, 1.06),
               handlelength=1.0, columnspacing=1.0)
    save(fig, "pipeline_latency_breakdown")


def main():
    style()
    cfg = load_config()
    runs = read_jsonl(RAW / "review_runs.jsonl") + read_jsonl(RAW / "extended_runs.jsonl")
    models = [m for m in MODEL_ORDER if any(r["model"] == m for r in runs)]
    scatter(runs, models, "request_latency_ms", statistics.median, 1 / 1000, "Review latency (s)",
            "pr_size_vs_latency")
    scatter(runs, models, "cost_usd_est", statistics.mean, 100, "Inference cost (cents)", "pr_size_vs_cost",
            logy=True)
    breakdown()
    print("figures written to", FIGURES)


if __name__ == "__main__":
    main()
