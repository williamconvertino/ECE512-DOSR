# DOSR demo

Runs the whole system on one computer (or two): a local EVM chain (Foundry's `anvil`) with the
**DOSRRegistry** contract, an IPFS node (Kubo), the attestor, and the client GUI. Everything is real
except the network: anvil uses prefunded test accounts (no real ETH) and mines one block per
transaction.

| Contents | |
|---|---|
| `demo.py` | launcher for every part of the demo (below) |
| `setup_env.py` | creates `demo/.venv` with all DOSR packages |
| `clients/*.json` | demo profiles: Alice, Bob, Carol (honest) and Mallory (asks for an unapproved model) |
| `projects/` | seed projects (`calculator-py`, `todo-js`, `secure-vault`) and the changes the scripted demo proposes |
| `templates/team-review-v1.toml` | a custom PR template used by `todo-js` |
| `start-*.bat` / `start-*.command` | one-click launchers (Windows / macOS) |

## Setup (once)

```bash
python demo/setup_env.py
```

Requires Python 3.10+ and git. `PY` below means `demo\.venv\Scripts\python.exe` (Windows) or
`demo/.venv/bin/python` (macOS/Linux). `anvil` and Kubo are downloaded into `demo/.foundry/` and
`demo/.ipfs/` on first use (checksums verified); to fetch them ahead of time:
`PY demo/demo.py chain install` and `PY demo/demo.py ipfs install`.

**LLM review (optional).** Put a key in the repository's `.env` (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GEMINI_API_KEY` or `ZHIPU_API_KEY`), or enter it in the GUI (*API keys*, top
right). `demo.py` loads `.env` for the attestor and the clients. The scripted demo then reviews
`calculator-py` with the first provider that has a key (override with
`DEMO_LLM_MODEL=provider/model@version`). Without a key, the mock reviewer is used and the LLM
scenario is skipped.

## One-click start

| Launcher | What it runs |
|---|---|
| `start-demo` | one client on this computer, no IPFS (publishing uses a local store) |
| `start-ipfs-demo` | one client with an IPFS node and a peer inbox; run it on two computers to share repositories |
| `start-two-clients` | two clients on this computer (Laptop A and Laptop B), each with its own attestor and IPFS node, already peered |

Double-click the `.bat` (Windows) or `.command` (macOS; first time: right-click, Open). Each creates
`demo/.venv` on first run.

## Scripted end-to-end demo (terminal)

```bash
PY demo/demo.py run --reset --start-attestor      # add --pause to step through scenarios
```

| # | Who | Scenario | Expected |
|---|---|---|---|
| 1 | Alice | create `calculator-py` (policy adds `docs/*.md` as review context) | created and registered on chain |
| 2 | Bob | add `power()` / `modulo()` | **approved**, canonical HEAD advances |
| 3 | Mallory | asks for `mock/always-approve@1` | **blocked** by her own client |
| 4 | Mallory | same, with `--force` | **rejected** by the attestor (signed) |
| 5 | Bob | work based on an old commit | stale parent, caught before anything is built |
| 6 | Bob | five tampered requests (stale parent, bundle checksum, wrong template, tampered prompt, modified policy), without and with `--force` | **blocked**, then **rejected** |
| 7 | Carol | `todo-js` with a minimum model version and the custom template; 2 commits in one PR; too-old model, forced, then a valid model | **blocked**, **rejected**, **approved** |
| 8 | Alice | `secure-vault`, whose strict policy does not trust this attestor | **blocked**, forced: **rejected** |
| 9 | Mallory | `evaluate()` using `eval()` plus a "pre-approved" comment, reviewed by the real LLM (needs a key) | **rejected by the model** |
| 10 | Carol on device B | `dosr clone calculator-py` from its on-chain snapshot | cloned and verified |
| 11 | Bob (A) vs Carol (B) | two approved PRs on the same HEAD, both sent in one block | **1 landed, 1 `StaleParent`** |
| 12 | the loser | `dosr verify`, `dosr sync` (rebase), resubmit | **approved** |
| 13 | both | `dosr verify` | both devices agree on one HEAD |

Scenarios 3-8 are stopped by deterministic checks before any model call. The run ends with a summary
table (expected vs actual outcome) and the chain's recent transactions. `--no-p2p` skips 10.

## Live GUI demo

```bash
PY demo/demo.py up        # chain + IPFS + attestor (:8080) + GUI (:8765), opens the browser
```

(or `PY demo/demo.py chain`, `PY demo/demo.py attestor` and `PY demo/demo.py gui` in separate
terminals; `--no-ipfs` skips IPFS.) Suggested walkthrough:

1. Pick a profile (top right). The pill shows attestor health; **⛓ EVM** opens the chain explorer.
2. **+ New**: create a repository from a seed (e.g. `todo-js`): choose approved models, PR template,
   extra context files and limits. This writes the immutable `.dosr/policy.json`, makes the genesis
   commit and registers it on the chain.
3. **Files** / **Changes**: edit a file and review the diff against the canonical HEAD.
4. **Submit PR** (as another profile): watch each pipeline step with its latency; then the **Checks**
   card (client vs attestor, side by side), the **Signed attestation**, and **Artifacts** (the exact
   prompt sent to the attestor).
5. **Attack demos**: choose a bad-request mode (e.g. `tampered-prompt`) and submit: blocked by the
   client. Tick **Force send** and submit again: rejected by the attestor.
6. **History** and **Chain** tabs: every submission, and this clone verified against the chain.

## Two clients, IPFS and the race demo

```bash
PY demo/demo.py pair      # Laptop A (http://127.0.0.1:8765) and Laptop B (:8766), already peered
```

- **Share:** create a repository on A; on B, ⛓ → *Clone* (from the chain) or import its share code
  (P2P → Inbox). Submit on B; A pulls B's signed change.
- **Race:** on both, untick *Register approval on chain* and submit: both are approved for the same
  parent. *Land on chain* on A, then on B: B's transaction reverts with `StaleParent`. On B,
  *Sync & rebase*, then submit again.

**Two real computers.** On each, run `start-ipfs-demo` (or `PY demo/demo.py up`) and add the other as a
peer (P2P pill → *Your address* on one, *Add peer* on the other; same Wi-Fi/LAN). To share one chain,
start it on one computer with `PY demo/demo.py chain --host 0.0.0.0` and set
`DOSR_RPC_URL=http://<host-ip>:8545` on both before `demo.py up`.

## Other commands

| Command | Purpose |
|---|---|
| `PY demo/demo.py reset` | delete `demo/workspace/` (repositories + attestor cache) and reset the dev chain (`--keep-chain` keeps it) |
| `PY demo/demo.py --device b reset` | the same for Laptop B |
| `PY demo/demo.py chain status` / `chain reset` | dev chain status / fresh chain (contract redeployed at the same address) |
| `PY demo/demo.py ipfs status` | IPFS node status |
| `PY demo/demo.py --chain mock ...` | use a per-device JSON-file mock chain instead of anvil |

Default ports: attestor 8080, GUI 8765, peer inbox 8790, chain 8545, IPFS API 5001 / swarm 4001 /
gateway 8089 (Laptop B: each +1).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Approved PRs fail at *Register on chain* with "attestation is issued in the future" | The dev chain was restarted from a saved state whose block clock is behind real time. Run `PY demo/demo.py reset` (or `PY demo/demo.py chain reset`) and start again. |
| Approvals rejected with `git fetch failed` / `unpack-objects failed` / "Filename too long" (Windows) | The workspace path is too long for Git on Windows (260 characters). Move the repository to a short path such as `C:\ECE512-DOSR`. |
| Pill says **chain down** | `PY demo/demo.py chain`; check that port 8545 is free. |
| Pill says *local store (no IPFS)* | `PY demo/demo.py ipfs install`, then restart `demo.py up`. |
| *Add peer* fails | Both computers on the same network; allow Python (port 8790) and `ipfs.exe` (port 4001) in the firewall. Share codes can also be pasted by hand. |
| Port in use | Stop the other process, or pass `--attestor-port` / `--port`. |
