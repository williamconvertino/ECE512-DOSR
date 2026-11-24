import json
import random
import re
import subprocess
from collections import defaultdict

from common import DATA, REPOS, load_config

GIT = ["git", "-c", "core.autocrlf=false", "-c", "core.quotepath=false"]


def git(repo, *args) -> str:
    return subprocess.run([*GIT, "--git-dir", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def ensure_clone(name: str, url: str):
    path = REPOS / f"{name}.git"
    if not path.exists():
        REPOS.mkdir(parents=True, exist_ok=True)
        subprocess.run([*GIT, "clone", "-q", "--bare", url, str(path)], check=True)
    return path


def commits(repo) -> list[dict]:
    out = git(repo, "log", "--first-parent", "--no-merges", "-M", "--numstat",
              "--format=@@@%H %P%x09%ct%x09%s", "HEAD")
    rows, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@@"):
            head, ts, subject = line[3:].split("\t", 2)
            shas = head.split()
            cur = {"candidate": shas[0], "parents": shas[1:], "time": int(ts), "subject": subject, "files": []}
            rows.append(cur)
        elif line.strip() and cur is not None:
            a, d, path = line.split("\t", 2)
            cur["files"].append((a, d, path))
    return [r for r in rows if len(r["parents"]) == 1 and r["files"]]


def eligible(c: dict, f: dict) -> str | None:
    if re.search(f["exclude_message_regex"], c["subject"]):
        return "message"
    if len(c["files"]) > f["max_changed_files"]:
        return "too many files"
    src = tot = 0
    for a, d, path in c["files"]:
        if a == "-" or d == "-":
            return "binary"
        if "=>" in path:
            return "rename"
        if re.search(f["exclude_path_regex"], "/" + path):
            return "excluded path"
        n = int(a) + int(d)
        tot += n
        if any(path.endswith(ext) for ext in f["source_extensions"]):
            src += n
    if tot == 0 or src / tot < f["min_source_line_share"]:
        return "not mostly source"
    return None


def file_sizes_ok(repo, c: dict, limit: int) -> bool:
    for _, _, path in c["files"]:
        for rev in (c["candidate"], c["parents"][0]):
            try:
                size = int(git(repo, "cat-file", "-s", f"{rev}:{path}").strip())
            except subprocess.CalledProcessError:
                continue
            if size > limit:
                return False
    return True


def prompt_estimate(repo, name: str, c: dict, template) -> dict:
    from dosr_git import GitRepo, pr_inputs
    from dosr_protocol.decision import build_prompt
    from dosr_protocol.pr_format import build_pr, render_markdown, text_metrics

    g = GitRepo(repo)
    inputs = pr_inputs(g, template, c["parents"][0], c["candidate"])
    pr = build_pr(template=template, repo={"name": name, "repo_id": "0x" + "00" * 32},
                  parent_git_oid="sha1:" + c["parents"][0], candidate_git_oid="sha1:" + c["candidate"], **inputs)
    prompt = build_prompt(template, render_markdown(pr, template), "dosr-review-decision-v1")
    m = text_metrics(prompt)
    return {"prompt_chars": len(prompt), "prompt_est_tokens": m["est_tokens"],
            "patch_bytes": len(inputs["diff_text"].encode("utf-8")),
            "context_files": len(inputs["context_files"])}


def main():
    from dosr_protocol.templates import load_template_file

    from common import ROOT

    cfg = load_config()
    f = cfg["filters"]
    template = load_template_file(ROOT / cfg["template_file"])
    rng = random.Random(cfg["seed"])
    buckets = [tuple(b) for b in cfg["buckets"]]

    pool = defaultdict(lambda: defaultdict(list))
    stats = defaultdict(lambda: defaultdict(int))
    for name, url in cfg["repos"].items():
        repo = ensure_clone(name, url)
        for c in commits(repo):
            why = eligible(c, f)
            stats[name][why or "eligible"] += 1
            if why:
                continue
            added = sum(int(a) for a, _, _ in c["files"])
            deleted = sum(int(d) for _, d, _ in c["files"])
            size = added + deleted
            b = next((b for b in buckets if b[0] <= size <= b[1]), None)
            if b:
                c.update(repo=name, url=url, added=added, deleted=deleted, changed=size)
                pool[b][name].append(c)
    for b in buckets:
        for name in pool[b]:
            pool[b][name].sort(key=lambda c: c["candidate"])
            rng.shuffle(pool[b][name])

    pool_sizes = {f"{b[0]}-{b[1]}": {n: len(v) for n, v in pool[b].items()} for b in buckets}
    chosen = []
    skipped = defaultdict(int)
    for b in buckets:
        names = sorted(pool[b])
        rng.shuffle(names)
        picked, per_repo = [], defaultdict(int)
        progress = True
        while len(picked) < cfg["per_bucket"] and progress:
            progress = False
            for name in names:
                if len(picked) >= cfg["per_bucket"]:
                    break
                if per_repo[name] >= cfg["max_per_repo_per_bucket"]:
                    continue
                cands = pool[b][name]
                while cands:
                    c = cands.pop(0)
                    repo = REPOS / f"{name}.git"
                    if not file_sizes_ok(repo, c, f["max_file_bytes"]):
                        skipped["large file"] += 1
                        continue
                    est = prompt_estimate(repo, name, c, template)
                    if est["prompt_est_tokens"] > f["max_prompt_est_tokens"]:
                        skipped["prompt too large"] += 1
                        continue
                    c.update(est)
                    picked.append(c)
                    per_repo[name] += 1
                    progress = True
                    break
        if len(picked) < cfg["per_bucket"]:
            print(f"warning: bucket {b} has only {len(picked)} commits")
        for c in picked:
            c["bucket"] = f"{b[0]}-{b[1]}"
        chosen += sorted(picked, key=lambda c: c["changed"])

    ps = cfg["pipeline_subset"]
    for c in chosen:
        c["pipeline_size"] = None
    for label in ("small", "medium"):
        target = ps[label]["target"]
        for c in sorted((c for c in chosen if c["pipeline_size"] is None),
                        key=lambda c: (abs(c["changed"] - target), c["candidate"]))[:ps[label]["pick"]]:
            c["pipeline_size"] = label
    lo, hi = ps["large"]["range"]
    large = sorted((c for c in chosen if lo <= c["changed"] <= hi), key=lambda c: c["candidate"])
    rng.shuffle(large)
    for c in large[:ps["large"]["pick"]]:
        c["pipeline_size"] = "large"

    DATA.mkdir(parents=True, exist_ok=True)
    with (DATA / "workload_manifest.jsonl").open("w", encoding="utf-8", newline="\n") as out:
        for i, c in enumerate(chosen):
            msg = git(REPOS / f"{c['repo']}.git", "log", "-1", "--format=%B", c["candidate"]).strip()
            row = {
                "pr_id": f"{c['bucket']}-{c['repo']}-{c['candidate'][:10]}",
                "repo": c["repo"], "url": c["url"], "parent": c["parents"][0], "candidate": c["candidate"],
                "subject": c["subject"], "message": msg, "commit_time": c["time"],
                "bucket": c["bucket"], "added": c["added"], "deleted": c["deleted"], "changed": c["changed"],
                "files": len(c["files"]), "paths": [p for _, _, p in c["files"]],
                "patch_bytes": c["patch_bytes"], "prompt_chars": c["prompt_chars"],
                "prompt_est_tokens": c["prompt_est_tokens"], "context_files": c["context_files"],
                "pipeline_size": c["pipeline_size"],
            }
            out.write(json.dumps(row) + "\n")
    meta = {"seed": cfg["seed"], "filter_stats": {k: dict(v) for k, v in stats.items()},
            "pool_sizes": pool_sizes,
            "skipped_at_pick": dict(skipped), "chosen": len(chosen)}
    (DATA / "workload_sampling.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    for c in chosen:
        print(f"{c['bucket']:>10} {c['repo']:<9} {c['candidate'][:10]} {c['changed']:>5} lines {len(c['files']):>3} files "
              f"~{c['prompt_est_tokens']:>7,} tok  {c['pipeline_size'] or '':<6} {c['subject'][:60]}")


if __name__ == "__main__":
    main()
