import csv
import time

from common import CACHE, RESULTS, ROOT, load_config, write_json
from harness import AttestorServer, Chain, Kubo, Probe, TimedChain, TimedStore, build_attestor

WORK = CACHE / "work-gas"


def main():
    import shutil

    from dosr_client import p2p
    from dosr_client.attestor import AttestorClient
    from dosr_client.config import ClientProfile, DosrRepo
    from dosr_client.ipfs import IpfsStore
    from dosr_client.pipeline import SubmitOptions, parse_model_choice, submit
    from dosr_client.repo_ops import create_repository

    from benchmark_review import build_policy_for

    cfg = load_config()
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    models = ["mock/deterministic-reviewer-v1@1"] + cfg["models"]["primary"] + cfg["models"]["extended"]
    policy = build_policy_for(cfg, models)
    profile = ClientProfile(id="bench", name="DOSR Bench", email="bench@dosr.invalid")
    rows = []

    with Chain() as ch, Kubo(CACHE / "ipfs-gas") as kubo:
        rpc = ch.rpc
        blk = rpc("eth_getBlockByNumber", "0x1", False)
        rcpt = rpc("eth_getTransactionReceipt", blk["transactions"][0])
        code = rpc("eth_getCode", ch.contract, "latest")
        rows.append({"operation": "deploy", "occurrence": "first (contract creation)", "seq": 1,
                     "gas_used": int(rcpt["gasUsed"], 16), "calldata_note": f"runtime code {len(code) // 2 - 1} bytes"})

        probe = Probe()
        att = build_attestor(probe, WORK / "attestor-cache", ch.rpc_url, ch.contract, ROOT / cfg["providers_file"])
        with AttestorServer(att) as server:
            client = AttestorClient(server.url, timeout=300)
            for i, name in enumerate(["requests", "pytest", "seaborn"]):
                seed = WORK / f"seed-{i}"
                (seed / "pkg").mkdir(parents=True)
                (seed / "README.md").write_text(f"# {name}\n\nGas benchmark repository.\n", encoding="utf-8")
                (seed / "pkg" / "mod.py").write_text("def f(x):\n    return x\n", encoding="utf-8")
                chain = ch.chain(2)
                created = create_repository(WORK / f"repo-{i}", name=name, policy=policy, profile=profile,
                                            chain=chain, seed_dir=seed)
                rows.append({"operation": "registerRepository",
                             "occurrence": "first repository in registry" if i == 0 else "subsequent repository",
                             "seq": i + 1, "gas_used": created["registration"]["gas_used"],
                             "calldata_note": f"name '{name}', 1 attestor"})
                if i:
                    continue
                repo = DosrRepo(created["path"])
                timed = TimedChain(chain)
                store = TimedStore(IpfsStore(kubo.api))
                for k in range(3):
                    p = repo.path / "pkg" / "mod.py"
                    p.write_text(p.read_text() + f"\n\ndef g{k}(x):\n    return x + {k}\n", encoding="utf-8")
                    sub = submit(repo, profile, client, chain,
                                 SubmitOptions(message=f"Add g{k}", model=parse_model_choice(models[0])),
                                 publisher=lambda r: p2p.publish(r, timed, store, profile))
                    d = sub.to_dict()
                    assert d["outcome"] == "approved", d["errors"]
                    ev = d["info"]["chain_event"]
                    rows.append({"operation": "acceptTransition",
                                 "occurrence": "first PR of repository" if k == 0 else "subsequent PR",
                                 "seq": k + 1, "gas_used": ev["gas_used"], "calldata_note": "1 attestation + signature"})
                    snap = timed.snapshot_calls[-1]
                    rows.append({"operation": "publishSnapshot",
                                 "occurrence": "first snapshot of repository" if k == 0 else "subsequent snapshot (overwrite)",
                                 "seq": k + 1, "gas_used": snap["gas_used"],
                                 "calldata_note": f"CIDv1 string of {snap['cid_len']} chars"})
                    time.sleep(1.1)

    with (RESULTS / "gas_results.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    g = cfg["gas"]
    est = []
    for r in rows:
        for gwei in g["gwei_scenarios"]:
            eth = r["gas_used"] * gwei * 1e-9
            est.append({**{k: r[k] for k in ("operation", "occurrence", "seq", "gas_used")},
                        "gas_price_gwei": gwei, "cost_eth": eth, "cost_usd": eth * g["eth_usd"],
                        "eth_usd": g["eth_usd"], "primary_scenario": gwei == g["primary_gwei"]})
    with (RESULTS / "gas_cost_estimates.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(est[0]))
        w.writeheader()
        w.writerows(est)
    write_json(RESULTS / "gas_meta.json", {"eth_usd": g["eth_usd"], "eth_usd_source": g["eth_usd_source"],
                                           "gwei_scenarios": g["gwei_scenarios"], "primary_gwei": g["primary_gwei"],
                                           "label": "estimated transaction costs (local anvil gas x assumed gas price); "
                                                    "no fees were paid"})
    for r in rows:
        print(f"{r['operation']:<20} {r['occurrence']:<36} {r['gas_used']:>9,}")


if __name__ == "__main__":
    main()
