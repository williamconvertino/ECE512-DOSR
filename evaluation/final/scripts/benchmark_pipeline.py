import csv
import statistics
from collections import defaultdict

from common import RAW, RESULTS, load_config, read_jsonl

STAGES = [
    ("client_prep_ms", "1. Client preparation and Git bundling"),
    ("attestor_validation_ms", "2. Attestor validation and prompt construction"),
    ("provider_inference_ms", "3. Model inference"),
    ("parse_sign_verify_ms", "4. Response parsing, attestation signing (+ client verification)"),
    ("evm_accept_ms", "5. EVM acceptance transaction and receipt"),
    ("ipfs_publish_ms", "6. IPFS bundle publication"),
    ("evm_snapshot_ms", "7. EVM snapshot locator transaction and receipt"),
]
SIZES = ["small", "medium", "large"]


def pipeline_row(r: dict, cfg: dict) -> dict:
    gwei, eth_usd = cfg["gas"]["primary_gwei"], cfg["gas"]["eth_usd"]
    gas = (r.get("gas_accept") or 0) + (r.get("gas_snapshot") or 0)
    approved = r["outcome"] == "approved"
    tx_usd = gas * gwei * 1e-9 * eth_usd if approved else 0.0
    row = {
        "pr_id": r["pr_id"], "repo": r["repo"], "pipeline_size": r["pipeline_size"], "model": r["model"],
        "rep": r["rep"], "outcome": r["outcome"],
        "changed_lines": (r["lines_added"] or 0) + (r["lines_deleted"] or 0), "changed_files": r["changed_files"],
        "patch_bytes": r["patch_bytes"], "bundle_bytes": r["bundle_bytes"],
        "publish_bundle_bytes": r.get("publish_bundle_bytes"),
        "client_prep_ms": r["client_prep_ms"],
        "attestor_validation_ms": (r["attestor_pre_inference_ms"] or 0) + (r.get("http_overhead_ms") or 0),
        "attestor_pre_inference_ms": r["attestor_pre_inference_ms"], "http_overhead_ms": r.get("http_overhead_ms"),
        "provider_inference_ms": r["provider_inference_ms"],
        "parse_sign_verify_ms": (r["attestor_post_inference_ms"] or 0) + (r["client_verify_ms"] or 0),
        "attestor_post_inference_ms": r["attestor_post_inference_ms"], "client_verify_ms": r["client_verify_ms"],
        "evm_accept_ms": r["evm_accept_ms"], "ipfs_publish_ms": r["ipfs_publish_ms"],
        "ipfs_add_ms": sum(a["ms"] for a in r.get("ipfs_adds") or []) if r.get("ipfs_adds") else None,
        "evm_snapshot_ms": r["evm_snapshot_ms"],
        "request_latency_ms": r["request_latency_ms"],
        "total_wall_ms": r["wall_ms"], "pipeline_elapsed_ms": r["pipeline_elapsed_ms"],
        "inference_cost_usd_est": r["cost_usd_est"],
        "gas_accept": r.get("gas_accept"), "gas_snapshot": r.get("gas_snapshot"), "gas_total": gas if approved else 0,
        "tx_cost_usd_est_1gwei": tx_usd,
        "total_cost_usd_est": (r["cost_usd_est"] or 0) + tx_usd,
        "prompt_tokens": r["prompt_tokens"], "output_tokens": r["output_tokens"],
    }
    row["sum_of_stages_ms"] = sum(row[c] or 0 for c, _ in STAGES) if approved else None
    row["total_wall_excl_evm_ms"] = (row["total_wall_ms"] - (row["evm_accept_ms"] or 0)
                                     - (row["evm_snapshot_ms"] or 0))
    return row


def main():
    cfg = load_config()
    runs = [r for r in read_jsonl(RAW / "review_runs.jsonl")
            if r.get("pipeline_size") and r["model"] in cfg["models"]["primary"]]
    rows = [pipeline_row(r, cfg) for r in runs]
    rows.sort(key=lambda r: (SIZES.index(r["pipeline_size"]), r["model"], r["pr_id"], r["rep"]))
    if not rows:
        print("no runs yet")
        return
    with (RESULTS / "pipeline_runs.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    groups = defaultdict(list)
    for r in rows:
        groups[(r["model"], r["pipeline_size"])].append(r)
    summary = []
    for model in cfg["models"]["primary"]:
        for size in SIZES:
            all_rs = groups.get((model, size), [])
            rs = [r for r in all_rs if r["outcome"] == "approved"]
            med = (lambda c: statistics.median(r[c] for r in rs) if rs else None)
            s = {"model": model, "pipeline_size": size, "prs": len({r["pr_id"] for r in all_rs}),
                 "runs": len(all_rs), "approved_runs": len(rs),
                 "rejected_runs": sum(r["outcome"] == "rejected" for r in all_rs),
                 "approved_prs": len({r["pr_id"] for r in rs}),
                 "changed_lines_range": (f"{min(r['changed_lines'] for r in all_rs)}-"
                                         f"{max(r['changed_lines'] for r in all_rs)}") if all_rs else "",
                 "median_bundle_bytes": med("bundle_bytes"), "median_publish_bundle_bytes": med("publish_bundle_bytes")}
            for c, _ in STAGES:
                s[f"median_{c}"] = med(c)
            s.update({
                "median_total_wall_ms": med("total_wall_ms"),
                "median_total_wall_excl_evm_ms": med("total_wall_excl_evm_ms"), "median_sum_of_stages_ms": med("sum_of_stages_ms"),
                "median_request_latency_ms": med("request_latency_ms"),
                "mean_inference_cost_usd_est": statistics.mean(r["inference_cost_usd_est"] for r in rs) if rs else None,
                "median_gas_accept": med("gas_accept"), "median_gas_snapshot": med("gas_snapshot"),
                "mean_tx_cost_usd_est_1gwei": statistics.mean(r["tx_cost_usd_est_1gwei"] for r in rs) if rs else None,
                "mean_total_cost_usd_est": statistics.mean(r["total_cost_usd_est"] for r in rs) if rs else None,
            })
            summary.append(s)
    with (RESULTS / "pipeline_summary.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0]))
        w.writeheader()
        w.writerows(summary)
    for s in summary:
        print(s["model"].split("/")[1], s["pipeline_size"], s["approved_runs"], "/", s["runs"],
              s["median_total_wall_ms"])


if __name__ == "__main__":
    main()
