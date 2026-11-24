import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import io
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import common
from common import CACHE, DATA, RAW, ROOT, cost_usd, environment, load_config, load_dotenv, read_jsonl, write_json
from harness import TimedChain, TimedStore, infrastructure

WORK = CACHE / "work"
GIT = ["git", "-c", "core.autocrlf=false"]

CLIENT_PREP = ("preflight", "commit", "parent", "bundle", "pr", "prompt", "request", "validate")


def src_git(repo: str, *args, text=True):
    return subprocess.run([*GIT, "--git-dir", str(common.REPOS / f"{repo}.git"), *args], check=True,
                          capture_output=True, text=text).stdout


def extract_tree(pr: dict, dest: Path) -> None:
    data = src_git(pr["repo"], "archive", "--format=tar", pr["parent"], text=False)
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        members = [m for m in tf.getmembers() if m.isfile() or m.isdir()]
        tf.extractall(dest, members=members)


def changes(pr: dict) -> list[tuple[str, str]]:
    out = src_git(pr["repo"], "diff", "--name-status", "--no-renames", pr["parent"], pr["candidate"])
    return [tuple(l.split("\t", 1)) for l in out.splitlines() if l.strip()]


def apply_candidate(pr: dict, repo_path: Path, chg) -> None:
    for status, path in chg:
        target = repo_path / path
        if status == "D":
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src_git(pr["repo"], "show", f"{pr['candidate']}:{path}", text=False))


def build_policy_for(cfg, models):
    from dosr_protocol.policy import build_policy, parse_model_spec
    from dosr_protocol.templates import parse_template_text

    template = parse_template_text((ROOT / cfg["template_file"]).read_text("utf-8"), cfg["template_file"])
    p = cfg["policy"]
    return build_policy(p["preset"], repo_name="bench", approved_models=[parse_model_spec(m) for m in models],
                        template=template, max_changed_files=p["max_changed_files"],
                        max_patch_bytes=p["max_patch_bytes"])


class PRSession:

    def __init__(self, pr, infra, policy, profile):
        from dosr_client.config import DosrRepo
        from dosr_client.repo_ops import create_repository

        self.pr, self.infra, self.profile = pr, infra, profile
        base = WORK / pr["pr_id"]
        if base.exists():
            shutil.rmtree(base, onerror=_writable)
        extract_tree(pr, base / "seed")
        chain = infra["chain"].chain(2)
        created = create_repository(base / "repo", name=pr["repo"], policy=policy, profile=profile, chain=chain,
                                    seed_dir=base / "seed", description=f"DOSR benchmark at {pr['parent'][:10]}")
        self.registration_gas = (created.get("registration") or {}).get("gas_used")
        self.repo = DosrRepo(created["path"])
        self.genesis = self.repo.git.head()
        self.changes = changes(pr)
        self.snap = infra["chain"].snapshot()

    def reset(self):
        ch = self.infra["chain"]
        ch.revert(self.snap)
        self.snap = ch.snapshot()
        g = self.repo.git
        g.run("reset", "-q", "--hard", self.genesis)
        g.run("clean", "-qfdx")
        (self.repo.state_dir / "p2p.json").unlink(missing_ok=True)
        apply_candidate(self.pr, self.repo.path, self.changes)


def _writable(func, path, _):
    import os
    import stat

    os.chmod(path, stat.S_IWRITE)
    func(path)


def one_run(session: PRSession, model_spec: str, cfg) -> dict:
    from dosr_client import p2p
    from dosr_client.attestor import AttestorClient
    from dosr_client.pipeline import SubmitOptions, parse_model_choice, submit

    infra = session.infra
    probe = infra["probe"]
    session.reset()
    chain = infra["chain"].chain(2)
    timed_chain = TimedChain(chain)
    from dosr_client.ipfs import IpfsStore

    store = TimedStore(IpfsStore(infra["kubo"].api, timeout=300))
    publish_info = {}

    def publisher(repo):
        t = time.perf_counter()
        out = p2p.publish(repo, timed_chain, store, session.profile)
        publish_info["total_ms"] = (time.perf_counter() - t) * 1000
        return out

    client = AttestorClient(infra["server"].url, timeout=900)
    probe.reset()
    opts = SubmitOptions(message=session.pr["message"], model=parse_model_choice(model_spec))
    wall0 = time.perf_counter()
    sub = submit(session.repo, session.profile, client, chain, opts, publisher=publisher)
    wall_ms = (time.perf_counter() - wall0) * 1000
    d = sub.to_dict()
    steps = {s["id"]: s for s in d["steps"]}
    dur = {k: (v["duration_ms"] if v["status"] == "done" else None) for k, v in steps.items()}
    info, dec = d["info"], d.get("decision") or {}
    t = probe.t
    pr_stats = info.get("pr") or {}
    usage = probe.usage
    rec = {
        "pr_id": session.pr["pr_id"], "repo": session.pr["repo"], "parent": session.pr["parent"],
        "candidate": session.pr["candidate"], "bucket": session.pr["bucket"],
        "pipeline_size": session.pr.get("pipeline_size"),
        "model": model_spec, "template": cfg["template_id"],
        "outcome": d["outcome"],
        "approved": dec.get("approved"), "decided_by": dec.get("decided_by"),
        "model_output_valid": next((c["ok"] for c in info.get("attestor_checks", [])
                                    if c["name"] == "model_output_is_valid"), None),
        "summary": dec.get("summary"), "issues": dec.get("issues"),
        "errors": d["errors"], "warnings": d["warnings"],
        "lines_added": pr_stats.get("additions"), "lines_deleted": pr_stats.get("deletions"),
        "changed_files": pr_stats.get("changed_files"), "patch_bytes": pr_stats.get("patch_bytes"),
        "source_added": session.pr["added"], "source_deleted": session.pr["deleted"],
        "prompt_bytes": (info.get("prompt") or {}).get("bytes"),
        "prompt_est_tokens": (info.get("prompt") or {}).get("est_tokens"),
        "bundle_bytes": (info.get("bundle") or {}).get("size_bytes"), "bundle_mode": (info.get("bundle") or {}).get("mode"),
        "upload_total_bytes": (info.get("transfer") or {}).get("upload_total_bytes"),
        "prompt_tokens": (usage or {}).get("prompt_tokens"), "output_tokens": (usage or {}).get("completion_tokens"),
        "cached_tokens": (usage or {}).get("cached_tokens"), "reasoning_tokens": (usage or {}).get("reasoning_tokens"),
        "usage_raw": (usage or {}).get("raw"),
        "request_latency_ms": (dec.get("upload_ms") or 0) + (dec.get("server_wait_ms") or 0) if dec else None,
        "upload_ms": dec.get("upload_ms"), "server_wait_ms": dec.get("server_wait_ms"),
        "attestor_review_ms": _span(t, "review_start", "review_end"),
        "attestor_pre_inference_ms": _span(t, "review_start", "infer_start"),
        "provider_inference_ms": _span(t, "infer_start", "infer_end"),
        "attestor_post_inference_ms": _span(t, "infer_end", "review_end"),
        "provider_http_status": probe.provider_status, "provider_error": probe.provider_error,
        "step_ms": dur,
        "client_prep_ms": sum(dur[k] or 0 for k in CLIENT_PREP) if all(dur.get(k) is not None for k in CLIENT_PREP) else None,
        "client_verify_ms": dur.get("verify"),
        "evm_accept_ms": dur.get("register"),
        "publish_step_ms": dur.get("publish"),
        "ipfs_adds": store.adds,
        "evm_snapshot_ms": timed_chain.snapshot_calls[0]["ms"] if timed_chain.snapshot_calls else None,
        "gas_accept": (info.get("chain_event") or {}).get("gas_used"),
        "gas_snapshot": timed_chain.snapshot_calls[0]["gas_used"] if timed_chain.snapshot_calls else None,
        "share_cid": (info.get("published") or {}).get("share"),
        "publish_bundle_bytes": ((info.get("published") or {}).get("bundle") or {}).get("size_bytes"),
        "pipeline_elapsed_ms": d["elapsed_ms"], "wall_ms": wall_ms,
        "registration_gas": session.registration_gas,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if rec["publish_step_ms"] is not None and rec["evm_snapshot_ms"] is not None:
        rec["ipfs_publish_ms"] = rec["publish_step_ms"] - rec["evm_snapshot_ms"]
    else:
        rec["ipfs_publish_ms"] = None
    if rec["request_latency_ms"] and rec["attestor_review_ms"] is not None:
        rec["http_overhead_ms"] = rec["request_latency_ms"] - rec["attestor_review_ms"]
    rec["cost_usd_est"] = cost_usd(model_spec, usage, cfg["pricing"])
    rec["status"] = ("completed" if rec["outcome"] in ("approved", "rejected") and dec.get("model_called")
                     else "provider_error" if probe.provider_error else "failed")
    return rec


def _span(t, a, b):
    return (t[b] - t[a]) * 1000 if a in t and b in t else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["primary", "extended"], default="primary")
    ap.add_argument("--paid", action="store_true", help="actually call paid models")
    ap.add_argument("--pilot", action="store_true", help="mock reviewer on 2 PRs (free plumbing check)")
    ap.add_argument("--only", nargs="*", help="pr_id prefixes to run")
    ap.add_argument("--reps", type=int)
    ap.add_argument("--models", nargs="*")
    ap.add_argument("--out", default="review_runs")
    ap.add_argument("--isolate", help="separate work dir / IPFS repo suffix, so a second benchmark process "
                                      "can run alongside without sharing state")
    args = ap.parse_args()

    cfg = load_config()
    load_dotenv()
    from dosr_protocol.templates import load_template_file

    cfg["template_id"] = load_template_file(ROOT / cfg["template_file"]).id
    workload = read_jsonl(DATA / "workload_manifest.jsonl")
    all_models = cfg["models"]["primary"] + cfg["models"]["extended"]
    if args.pilot:
        models, reps, out_name = ["mock/deterministic-reviewer-v1@1"], 1, "pilot_mock_runs"
        workload = [w for w in workload if w["pipeline_size"] in ("small", "large")][:1] + \
                   [w for w in workload if w["pipeline_size"] == "large"][:1]
    else:
        if not args.paid:
            sys.exit("paid models: pass --paid")
        models = args.models or cfg["models"][args.tier]
        reps = args.reps or cfg["reps"][args.tier]
        out_name = args.out
    if args.only:
        workload = [w for w in workload if any(w["pr_id"].startswith(p) for p in args.only)]

    RAW.mkdir(parents=True, exist_ok=True)
    out_path = RAW / f"{out_name}.jsonl"
    err_path = RAW / f"{out_name.replace('_runs', '')}_errors.jsonl"
    done = {(r["pr_id"], r["model"], r["rep"]) for r in read_jsonl(out_path) if r["status"] == "completed"}
    spent = sum(r.get("cost_usd_est") or 0 for f in RAW.glob("*_runs.jsonl") for r in read_jsonl(f))
    print(f"{len(workload)} PRs x {len(models)} models x {reps} reps; {len(done)} already done; "
          f"spent so far (est.) ${spent:.3f}")

    from dosr_client.config import ClientProfile

    profile = ClientProfile(id="bench", name="DOSR Bench", email="bench@dosr.invalid")
    policy = build_policy_for(cfg, ["mock/deterministic-reviewer-v1@1"] + all_models)
    global WORK
    ipfs_repo = CACHE / "ipfs"
    if args.isolate:
        WORK = CACHE / f"work-{args.isolate}"
        ipfs_repo = CACHE / f"ipfs-{args.isolate}"
    WORK.mkdir(parents=True, exist_ok=True)
    meta = {"started_at": datetime.now(timezone.utc).isoformat(), "models": models, "reps": reps,
            "tier": "pilot" if args.pilot else args.tier, "environment": environment(),
            "policy_review": {k: v for k, v in policy["review"].items() if k != "template"},
            "template": cfg["template_id"], "providers_file": cfg["providers_file"]}
    write_json(RAW / f"{out_name}.meta.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json", meta)

    with infrastructure(ROOT / cfg["providers_file"], WORK, ipfs_repo) as infra:
        meta["ipfs_version"] = infra["kubo"].version()
        for pr in workload:
            todo = [(m, r) for r in range(reps) for m in models if (pr["pr_id"], m, r) not in done]
            if not todo:
                continue
            session = PRSession(pr, infra, policy, profile)
            for model, rep in todo:
                if spent > cfg["budget_usd"]:
                    sys.exit(f"budget reached (${spent:.2f})")
                for attempt in range(3):
                    try:
                        rec = one_run(session, model, cfg)
                    except Exception:
                        rec = {"pr_id": pr["pr_id"], "model": model, "status": "harness_error",
                               "traceback": traceback.format_exc()}
                    rec.update(rep=rep, attempt=attempt, process=args.isolate or "main")
                    if rec["status"] == "completed":
                        with out_path.open("a", encoding="utf-8") as f:
                            f.write(json.dumps(rec, default=str) + "\n")
                        spent += rec.get("cost_usd_est") or 0
                        print(f"  {pr['pr_id']:<36} {model.split('/')[1]:<24} r{rep} {rec['outcome']:<8} "
                              f"req {rec['request_latency_ms'] / 1000:6.1f}s  inf {rec['provider_inference_ms'] / 1000:6.1f}s "
                              f"tok {rec['prompt_tokens'] or 0:>7,}/{rec['output_tokens'] or 0:>5,}  "
                              f"${rec['cost_usd_est'] or 0:.4f}  wall {rec['wall_ms'] / 1000:5.1f}s  (total ${spent:.2f})",
                              flush=True)
                        break
                    with err_path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, default=str) + "\n")
                    print(f"  {pr['pr_id']} {model} r{rep} attempt {attempt}: {rec['status']} "
                          f"{(rec.get('provider_error') or rec.get('errors') or rec.get('traceback', ''))!s:.300}",
                          flush=True)
                    time.sleep(10 * (attempt + 1))
    print("done")


if __name__ == "__main__":
    main()
