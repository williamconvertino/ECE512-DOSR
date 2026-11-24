# DOSR client

The client creates DOSR repositories, turns local changes into PRs, sends them to an attestor for
review, and lands approved changes on the chain. It has a CLI (`dosr`) and a local web GUI
(`dosr gui`); both run the same submission pipeline:

```
preflight -> commit -> resolve canonical parent -> bundle -> build PR -> build prompt -> build request
-> validate locally -> send to attestor -> await decision -> verify attestation -> register on chain -> publish to IPFS
```

## Setup

From the repository root, either use the demo environment (recommended):

```bash
python demo/setup_env.py
```

or install the packages into your own virtualenv:

```bash
pip install -r services/attestor/requirements.txt
pip install -e packages/protocol -e packages/git-storage -e packages/chain -e "apps/client[dev]"
```

This installs the `dosr` command (`demo/.venv/Scripts/dosr.exe` on Windows, `demo/.venv/bin/dosr`
on macOS/Linux). `python -m dosr_client.cli` is equivalent.

## What the client needs

| Setting | Flag | Environment variable | Default |
|---|---|---|---|
| Attestor URL | `--attestor-url` | `DOSR_ATTESTOR_URL` | profile `attestor_url`, else `http://127.0.0.1:8080` |
| Chain kind | `--chain evm\|mock` | `DOSR_CHAIN` | `evm` if an RPC URL is set, else `mock` |
| EVM RPC URL | `--rpc-url` | `DOSR_RPC_URL` | `http://127.0.0.1:8545` |
| Registry contract | `--contract` | `DOSR_CONTRACT` | dev address `0x5FbDB2315678afecb367f032d93F642f64180aa3` |
| Mock chain file | `--chain-state` | `DOSR_CHAIN_STATE` | (implies `--chain mock`) |
| IPFS API | `--ipfs-api` | `DOSR_IPFS_API` | `http://127.0.0.1:5001` (falls back to a local store if no node runs) |
| Profile | `--profile` | `DOSR_PROFILE` | your git identity |
| Model API keys | | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `ZHIPU_API_KEY` | |

The CLI reads these from the environment; it does not load `.env` by itself (`demo/demo.py` does).
The attestor and the client must use the **same chain**. On a dev chain, gas is paid from one of
anvil's prefunded test accounts, chosen by `chain_account` in the profile (or
`DOSR_CHAIN_PRIVATE_KEY`).

A **profile** is a small JSON file describing who you are, e.g. `demo/clients/alice.json`:

```json
{"name": "Alice", "email": "alice@dosr.demo", "chain_account": 2}
```

Optional fields: `attestor_url`, `review_model` (`{"provider", "model", "version"}`), `bundle_mode`
(`full` or `thin`).

## Typical CLI session

Start a chain and an attestor first (simplest: `PY demo/demo.py chain` and `PY demo/demo.py attestor`
in two terminals; see [services/attestor/README.md](../../services/attestor/README.md)). Then:

```bash
export DOSR_RPC_URL=http://127.0.0.1:8545
export DOSR_ATTESTOR_URL=http://127.0.0.1:8080
export DOSR_PROFILE=demo/clients/alice.json

# Create a repository: genesis commit + immutable .dosr/policy.json, registered on the chain
dosr init my-calc --name my-calc --seed demo/projects/calculator-py/files --preset standard

cd my-calc
# ... edit files ...
dosr submit -m "Add double()"        # commit, review, and (if approved) land on the chain
dosr status                          # local vs canonical state, attestor health
dosr history                         # past submissions; `dosr show` for one in detail
dosr verify                          # check this clone against the chain
```

Policy presets for `dosr init --preset`:

| Preset | Review |
|---|---|
| `standard` | mock reviewer (approves anything that passes the checks), no API key needed |
| `llm-review` | real LLM review (security + correctness template) |
| `strict` | two pinned models, two attestors, tighter limits |

Useful `init` options: `--model provider/model@version` (approved model; repeatable; versions may be
ranges such as `^1.2.0` or `>=2024-08-06`), `--template ID|FILE` (PR template; a custom TOML file is
embedded in the policy), `--context-file GLOB` (extra files every PR must include),
`--max-changed-files`, `--max-patch-bytes`, `--attestor KEY_ID=0xADDRESS`. `dosr models` lists
common models; `dosr template list|show|new|check` manages PR templates.

## Other commands

| Command | Purpose |
|---|---|
| `dosr submit --dry-run` | build and validate everything locally, send nothing |
| `dosr submit --model openai/gpt-6-luna@latest` | pick the review model (must be approved by the policy) |
| `dosr submit --no-register`, then `dosr land` | get an approval now, put it on the chain later |
| `dosr submit --bad-request MODE [--force]` | demo attacks (tampered prompt, stale parent, ...): blocked by the client, or with `--force` rejected by the attestor |
| `dosr sync` | move to the chain's canonical HEAD (fetch its snapshot, rebase local work) |
| `dosr clone NAME` | clone a repository registered on the chain from its on-chain snapshot |
| `dosr publish` / `dosr import CODE` / `dosr pull CODE` | share a repository over IPFS with a share code |
| `dosr chain --activity` | chain explorer: repositories and recent transactions |
| `dosr p2p` | IPFS / local-store status |

## GUI

```bash
dosr gui --workspace demo/workspace --seeds demo/projects --profiles demo/clients
```

Opens `http://127.0.0.1:8765` with tabs for files, changes, PR submission (live step progress,
checks, signed attestation, artifacts), history, chain state and P2P sharing. API keys can be entered
in the GUI (*API keys*, top right). `PY demo/demo.py gui` starts it with the demo's settings.
