import csv
import json
import statistics
from collections import defaultdict

import pandas as pd

from common import FINAL, MODEL_LABELS, RAW, RESULTS, TABLES, environment, load_config, read_jsonl, write_json

BUCKET_ORDER = ["1-10", "11-50", "51-100", "101-250", "251-500", "501-1000", "1001-2000"]
SIZE_LABEL = {"small": "Small", "medium": "Medium", "large": "Large"}


def read_csv(name):
    p = RESULTS / name
    return list(csv.DictReader(p.open(newline=""))) if p.exists() else []


def pct(v, d=1):
    return "--" if v is None else f"{100 * float(v):.{d}f}\\%"


def p90(xs):
    xs = sorted(xs)
    if not xs:
        return None
    k = 0.9 * (len(xs) - 1)
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def review_tables(cfg):
    runs = read_jsonl(RAW / "review_runs.jsonl") + read_jsonl(RAW / "extended_runs.jsonl")
    if not runs:
        return None
    flat_cols = ["pr_id", "repo", "parent", "candidate", "bucket", "pipeline_size", "model", "template", "rep",
                 "attempt", "status", "outcome", "approved", "model_output_valid", "lines_added", "lines_deleted",
                 "changed_lines", "changed_files", "patch_bytes", "prompt_bytes", "prompt_est_tokens", "bundle_bytes",
                 "prompt_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "request_latency_ms",
                 "upload_ms", "server_wait_ms", "attestor_review_ms", "attestor_pre_inference_ms",
                 "provider_inference_ms", "attestor_post_inference_ms", "http_overhead_ms", "client_prep_ms",
                 "client_verify_ms", "evm_accept_ms", "ipfs_publish_ms", "evm_snapshot_ms", "gas_accept",
                 "gas_snapshot", "wall_ms", "pipeline_elapsed_ms", "cost_usd_est", "finished_at"]
    for r in runs:
        r["changed_lines"] = (r["lines_added"] or 0) + (r["lines_deleted"] or 0)
    with (RESULTS / "review_runs.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=flat_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(runs)

    models = [m for m in cfg["models"]["primary"] + cfg["models"]["extended"] if any(r["model"] == m for r in runs)]
    summary = []
    for model in models:
        for bucket in BUCKET_ORDER + ["all"]:
            rs = [r for r in runs if r["model"] == model and (bucket == "all" or r["bucket"] == bucket)]
            if not rs:
                continue
            lat = [r["request_latency_ms"] / 1000 for r in rs]
            inf = [r["provider_inference_ms"] / 1000 for r in rs]
            summary.append({
                "model": model, "model_label": MODEL_LABELS.get(model, model), "bucket": bucket,
                "prs": len({r["pr_id"] for r in rs}), "evaluations": len(rs),
                "mean_changed_lines": statistics.mean(r["changed_lines"] for r in rs),
                "approvals": sum(r["outcome"] == "approved" for r in rs),
                "approval_rate": sum(r["outcome"] == "approved" for r in rs) / len(rs),
                "unparseable_outputs": sum(r.get("model_output_valid") is False for r in rs),
                "median_latency_s": statistics.median(lat), "p90_latency_s": p90(lat),
                "median_provider_latency_s": statistics.median(inf), "p90_provider_latency_s": p90(inf),
                "median_attestor_overhead_s": statistics.median(
                    (r["request_latency_ms"] - r["provider_inference_ms"]) / 1000 for r in rs),
                "mean_cost_usd_est": statistics.mean(r["cost_usd_est"] for r in rs),
                "total_cost_usd_est": sum(r["cost_usd_est"] for r in rs),
                "mean_prompt_tokens": statistics.mean(r["prompt_tokens"] for r in rs),
                "mean_output_tokens": statistics.mean(r["output_tokens"] for r in rs),
                "mean_reasoning_tokens": statistics.mean(r.get("reasoning_tokens") or 0 for r in rs),
                "mean_cached_tokens": statistics.mean(r.get("cached_tokens") or 0 for r in rs),
                "long_context_priced_evals": sum(
                    1 for r in rs if (lc := cfg["pricing"][model].get("long_context"))
                    and r["prompt_tokens"] > lc["above_prompt_tokens"]),
            })
    with (RESULTS / "review_summary.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0]))
        w.writeheader()
        w.writerows(summary)

    corr = []
    df = pd.DataFrame(runs)
    df["request_latency_s"] = df["request_latency_ms"] / 1000
    df["provider_latency_s"] = df["provider_inference_ms"] / 1000
    pairs = [("changed_lines", "prompt_tokens"), ("changed_lines", "cost_usd_est"),
             ("changed_lines", "request_latency_s"), ("prompt_tokens", "cost_usd_est"),
             ("prompt_tokens", "request_latency_s"), ("output_tokens", "request_latency_s"),
             ("output_tokens", "cost_usd_est"), ("patch_bytes", "prompt_tokens"), ("changed_lines", "output_tokens")]
    for model in models:
        d = df[df["model"] == model]
        for a, b in pairs:
            corr.append({"model": model, "x": a, "y": b, "n": len(d),
                         "spearman": d[a].corr(d[b], method="spearman"), "pearson": d[a].corr(d[b])})
        import numpy as np

        X = np.column_stack([np.ones(len(d)), d["prompt_tokens"] / 1000, d["output_tokens"] / 1000])
        coef, *_ = np.linalg.lstsq(X, d["request_latency_s"].to_numpy(), rcond=None)
        pred = X @ coef
        ss = ((d["request_latency_s"] - pred) ** 2).sum()
        tot = ((d["request_latency_s"] - d["request_latency_s"].mean()) ** 2).sum()
        corr.append({"model": model, "x": "linear fit latency_s ~ 1 + prompt_ktok + output_ktok", "y": "request_latency_s",
                     "n": len(d), "spearman": None, "pearson": None,
                     "intercept_s": coef[0], "s_per_1k_prompt_tokens": coef[1], "s_per_1k_output_tokens": coef[2],
                     "r2": 1 - ss / tot})
    with (RESULTS / "review_correlations.csv").open("w", newline="") as f:
        keys = sorted({k for c in corr for k in c}, key=lambda k: list(corr[-1]).index(k) if k in corr[-1] else 99)
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(corr)

    errors = read_jsonl(RAW / "review_errors.jsonl") + read_jsonl(RAW / "extended_errors.jsonl")
    err_summary = defaultdict(int)
    for e in errors:
        err_summary[f"{e['model']} :: {e['status']}"] += 1
    return {"runs": runs, "summary": summary, "corr": corr, "errors": errors, "error_summary": dict(err_summary)}


def gas_tex(cfg):
    rows = read_csv("gas_results.csv")
    if not rows:
        return
    g = cfg["gas"]
    seen, out = set(), []
    for r in rows:
        key = (r["operation"], r["occurrence"])
        if key in seen:
            continue
        seen.add(key)
        same = [int(x["gas_used"]) for x in rows if (x["operation"], x["occurrence"]) == key]
        out.append((r["operation"], r["occurrence"], same))
    names = {"deploy": "Registry deployment", "registerRepository": "Repository initialization",
             "acceptTransition": "Accept approved PR", "publishSnapshot": "Publish IPFS snapshot locator"}
    lines = [r"\begin{table}[t]", r"\centering\small", r"\begin{tabular}{llrrr}", r"\toprule",
             r"Operation & Occurrence & Gas & ETH & USD \\", r"\midrule"]
    for op, occ, gs in out:
        gas = max(gs)
        eth = gas * g["primary_gwei"] * 1e-9
        gas_s = f"{gas:,}" if len(set(gs)) == 1 else f"{min(gs):,}--{max(gs):,}"
        lines.append(f"{names[op]} & {occ} & {gas_s} & {eth:.6f} & \\${eth * g['eth_usd']:.4f} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}",
              rf"\caption{{Gas measured on a local EVM (anvil); estimated cost at {g['primary_gwei']} gwei and "
              rf"\${g['eth_usd']:,.2f}/ETH. Estimates, not fees paid.}}", r"\label{tab:gas}", r"\end{table}"]
    (TABLES / "gas_costs.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")


def pipeline_tex():
    rows = read_csv("pipeline_summary.csv")
    if not rows:
        return
    lines = [r"\begin{table}[t]", r"\centering\small", r"\begin{tabular}{llrrrr}", r"\toprule",
             r"Model & PR size & Latency excl.\ EVM (s) & Inference (\textcent) & Est.\ tx (\textcent) & Total (\textcent) \\",
             r"\midrule"]
    for r in rows:
        if not r["approved_runs"] or r["approved_runs"] == "0":
            lines.append(f"{MODEL_LABELS[r['model']]} & {SIZE_LABEL[r['pipeline_size']]} & -- & -- & -- & -- \\\\")
            continue
        lines.append(f"{MODEL_LABELS[r['model']]} & {SIZE_LABEL[r['pipeline_size']]} "
                     f"& {float(r['median_total_wall_excl_evm_ms']) / 1000:.1f} "
                     f"& {100 * float(r['mean_inference_cost_usd_est']):.2f} "
                     f"& {100 * float(r['mean_tx_cost_usd_est_1gwei']):.2f} "
                     f"& {100 * float(r['mean_total_cost_usd_est']):.2f} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}",
              r"\caption{Approved submissions through the full DOSR pipeline: median wall-clock latency "
              r"excluding the EVM transactions (local IPFS node); mean inference cost at list price "
              r"and estimated transaction cost at 1 gwei.}", r"\label{tab:pipeline}", r"\end{table}"]
    (TABLES / "pipeline_summary.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")


def template_tex():
    rows = read_csv("template_summary.csv")
    lines = [r"\begin{table}[t]", r"\centering\small", r"\begin{tabular}{llrrr}", r"\toprule",
             r"Template & Model & Accuracy & False rejection & False acceptance \\", r"\midrule"]
    for r in rows:
        lines.append(f"{r['template']} & {r['model_label']} & {pct(r['accuracy'])} & "
                     f"{pct(r['false_rejection_rate'])} & {pct(r['false_acceptance_rate'])} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\caption{Overall review quality (118 PRs, 3 evaluations each).}",
              r"\label{tab:template-summary}", r"\end{table}"]
    (TABLES / "template_summary.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")

    cat = [r for r in read_csv("template_by_category.csv") if r["template"] == "C"]
    by = defaultdict(dict)
    order = []
    for r in cat:
        if r["category_label"] not in order:
            order.append(r["category_label"])
        by[r["category_label"]][r["model"]] = r
    lines = [r"\begin{table}[t]", r"\centering\small", r"\begin{tabular}{p{3.6cm}lrr}", r"\toprule",
             r"PR category & Desired & Luna approval & Haiku approval \\", r"\midrule"]
    for label in order:
        lu = by[label]["openai/gpt-6-luna@latest"]
        ha = by[label]["anthropic/claude-haiku-5-5@latest"]
        lines.append(f"{label} & {lu['desired_decision'].capitalize()} & "
                     f"{pct(lu['approval_rate'])} ({lu['approvals']}/{lu['completed_evaluations']}) & "
                     f"{pct(ha['approval_rate'])} ({ha['approvals']}/{ha['completed_evaluations']}) \\\\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\caption{Approval rate by PR category, Template C.}",
              r"\label{tab:template-category}", r"\end{table}"]
    (TABLES / "template_by_category.tex").write_text("\n".join(lines) + "\n", encoding="utf-8")


def concurrency_check():
    base = {r["pr_id"]: r for r in read_jsonl(RAW / "mock_local_runs.jsonl")}
    win = RAW / "extended_window.txt"
    if not base or not win.exists():
        return None
    sets = {"primary_contended (discarded, re-run)": read_jsonl(RAW / "review_runs_contended.jsonl"),
            "primary_final (uncontended)": read_jsonl(RAW / "review_runs.jsonl"),
            "extended (ran alongside primary)": read_jsonl(RAW / "extended_runs.jsonl")}
    fields = ["client_prep_ms", "attestor_pre_inference_ms", "attestor_post_inference_ms", "evm_accept_ms",
              "ipfs_publish_ms", "evm_snapshot_ms"]
    out = {"window_utc": win.read_text().split(), "sets": {}}
    for name, runs in sets.items():
        d = {"runs": len(runs)}
        for f in fields:
            ratios = [r[f] / base[r["pr_id"]][f] for r in runs
                      if r.get(f) is not None and base.get(r["pr_id"], {}).get(f)]
            d[f] = {"n": len(ratios), "median_ratio_to_baseline": statistics.median(ratios) if ratios else None}
        out["sets"][name] = d
    return out


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def markdown_tables(cfg) -> str:
    parts = []
    rs = read_csv("review_summary.csv")
    if rs:
        parts.append("### Review latency and cost by PR size (end-to-end client->attestor request)\n")
        parts.append(md_table(
            ["Model", "Changed lines", "PRs", "Evals", "Mean lines", "Median lat. (s)", "P90 lat. (s)",
             "Median provider (s)", "Mean cost (USD)", "Mean prompt tok", "Mean output tok", "Approved", "Unparseable"],
            [[r["model_label"], r["bucket"], r["prs"], r["evaluations"], f"{float(r['mean_changed_lines']):.0f}",
              f"{float(r['median_latency_s']):.2f}", f"{float(r['p90_latency_s']):.2f}",
              f"{float(r['median_provider_latency_s']):.2f}", f"{float(r['mean_cost_usd_est']):.5f}",
              f"{float(r['mean_prompt_tokens']):,.0f}", f"{float(r['mean_output_tokens']):,.0f}",
              f"{r['approvals']}/{r['evaluations']}", r["unparseable_outputs"]] for r in rs]))
    cs = read_csv("review_correlations.csv")
    if cs:
        parts.append("\n### Correlations (per evaluation)\n")
        parts.append(md_table(["Model", "x", "y", "n", "Spearman", "Pearson"],
                              [[MODEL_LABELS.get(c["model"], c["model"]), c["x"], c["y"], c["n"],
                                f"{float(c['spearman']):.2f}", f"{float(c['pearson']):.2f}"]
                               for c in cs if c.get("spearman")]))
        parts.append("\n" + md_table(["Model", "Fit", "Intercept (s)", "s per 1k prompt tok", "s per 1k output tok", "R2"],
                                     [[MODEL_LABELS.get(c["model"], c["model"]), c["x"], f"{float(c['intercept_s']):.2f}",
                                       f"{float(c['s_per_1k_prompt_tokens']):.4f}",
                                       f"{float(c['s_per_1k_output_tokens']):.3f}", f"{float(c['r2']):.2f}"]
                                      for c in cs if c.get("r2")]))
    gr = read_csv("gas_cost_estimates.csv")
    if gr:
        parts.append("\n### Gas (local anvil) and estimated cost\n")
        by = defaultdict(dict)
        for r in gr:
            by[(r["operation"], r["occurrence"], r["seq"], r["gas_used"])][r["gas_price_gwei"]] = float(r["cost_usd"])
        parts.append(md_table(["Operation", "Occurrence", "Seq", "Gas", "USD @0.1 gwei", "USD @1 gwei", "USD @10 gwei"],
                              [[k[0], k[1], k[2], f"{int(k[3]):,}", f"{v['0.1']:.5f}", f"{v['1']:.4f}", f"{v['10']:.3f}"]
                               for k, v in by.items()]))
    ps = read_csv("pipeline_summary.csv")
    if ps:
        parts.append("\n### Full pipeline, approved submissions (medians in ms; costs mean USD)\n")
        cols = ["median_client_prep_ms", "median_attestor_validation_ms", "median_provider_inference_ms",
                "median_parse_sign_verify_ms", "median_evm_accept_ms", "median_ipfs_publish_ms",
                "median_evm_snapshot_ms", "median_sum_of_stages_ms", "median_total_wall_ms"]

        def f(v, d=0):
            return "--" if v in ("", None) else f"{float(v):,.{d}f}"
        parts.append(md_table(["Model", "Size", "Lines", "Approved/runs", "Approved PRs", "1 Client", "2 Attestor",
                               "3 Inference", "4 Parse+sign+verify", "5 EVM accept", "6 IPFS", "7 EVM snapshot",
                               "Sum of stages", "Total wall"],
                              [[MODEL_LABELS[r["model"]], r["pipeline_size"], r["changed_lines_range"],
                                f"{r['approved_runs']}/{r['runs']}", f"{r['approved_prs']}/{r['prs']}"]
                               + [f(r[c]) for c in cols] for r in ps]))
        parts.append("\n" + md_table(["Model", "Size", "Total latency excl. EVM (s)", "Inference cost (USD)",
                                      "Est. tx cost @1 gwei (USD)", "Total (USD)", "Median bundle (KB)",
                                      "Median publish bundle (KB)", "Gas accept", "Gas snapshot"],
                                     [[MODEL_LABELS[r["model"]], r["pipeline_size"],
                                       f(float(r["median_total_wall_excl_evm_ms"]) / 1000, 2) if r["median_total_wall_excl_evm_ms"] else "--",
                                       f(r["mean_inference_cost_usd_est"], 5), f(r["mean_tx_cost_usd_est_1gwei"], 4),
                                       f(r["mean_total_cost_usd_est"], 4),
                                       f(float(r["median_bundle_bytes"]) / 1024) if r["median_bundle_bytes"] else "--",
                                       f(float(r["median_publish_bundle_bytes"]) / 1024) if r["median_publish_bundle_bytes"] else "--",
                                       f(r["median_gas_accept"]), f(r["median_gas_snapshot"])] for r in ps]))
    return "\n".join(parts) + "\n"


def main():
    cfg = load_config()
    TABLES.mkdir(exist_ok=True)
    rev = review_tables(cfg)
    gas_tex(cfg)
    pipeline_tex()
    template_tex()

    metas = []
    for p in sorted(RAW.glob("*.meta.*.json")):
        metas.append({"file": p.name, **json.loads(p.read_text("utf-8"))})
    run_files = list(RAW.glob("*_runs.jsonl")) + [RAW / "review_runs_contended.jsonl"]
    total_cost = sum(r.get("cost_usd_est") or 0 for f in run_files for r in read_jsonl(f))
    calls = sum(1 for f in run_files for r in read_jsonl(f) if not r["model"].startswith("mock/"))
    err_calls = sum(1 for f in RAW.glob("*_errors.jsonl") for r in read_jsonl(f))
    write_json(RESULTS / "experiment_metadata.json", {
        "config": cfg, "environment": environment(), "runs": metas,
        "new_api_calls_completed": calls, "failed_attempts_recorded": err_calls,
        "new_api_spend_usd_est_total": total_cost,
        "workload_sampling": json.loads((FINAL / "data" / "workload_sampling.json").read_text("utf-8")),
    })
    tsupp = json.loads((RESULTS / "template_supplementary.json").read_text("utf-8"))
    supp = {"template_evaluation": tsupp}
    if rev:
        supp["review_latency_cost"] = {"correlations": rev["corr"], "errors": rev["error_summary"],
                                       "error_records": [{k: e.get(k) for k in ("pr_id", "model", "rep", "attempt", "status",
                                                                                "provider_error", "errors")}
                                                         for e in rev["errors"]]}
    cc = concurrency_check()
    if cc:
        supp["concurrency_check"] = cc
        write_json(RESULTS / "concurrency_check.json", cc)
    gas_est = read_csv("gas_cost_estimates.csv")
    if gas_est:
        supp["gas_cost_all_scenarios"] = gas_est
    pilot = read_jsonl(RAW / "pilot_paid_runs.jsonl")
    if pilot:
        supp["pilot_paid_runs_excluded_from_results"] = [
            {k: r.get(k) for k in ("pr_id", "model", "outcome", "request_latency_ms", "prompt_tokens", "output_tokens",
                                   "cost_usd_est")} for r in pilot]
    write_json(RESULTS / "supplementary_results.json", supp)
    (RESULTS / "report_tables.md").write_text(markdown_tables(cfg), encoding="utf-8")
    print(f"tables written; total new-API spend (est.) ${total_cost:.2f} over {calls} calls")


if __name__ == "__main__":
    main()
