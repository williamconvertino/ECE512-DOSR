# DOSR: Decentralized Open-Source Review

DOSR lets anyone propose a change to a repository without a trusted maintainer. Each change (PR) is
reviewed by an **attestor**, which checks the request against the repository's immutable policy,
rebuilds the PR from the Git bundle, asks an approved LLM to review it, and signs the result. A
smart contract on an EVM chain (**DOSRRegistry**) holds each repository's canonical HEAD and moves it
only for a correctly signed approval whose parent is the current HEAD. Repository snapshots are
shared over **IPFS**.

## Repository layout

| Path | What it is | Details |
|---|---|---|
| `apps/client/` | DOSR client: `dosr` CLI and local web GUI | [apps/client/README.md](apps/client/README.md) |
| `services/attestor/` | Attestor server (FastAPI) | [services/attestor/README.md](services/attestor/README.md) |
| `contracts/` | `DOSRRegistry.sol` and its build script | [contracts/README.md](contracts/README.md) |
| `packages/protocol/` | Shared protocol code: policies, PR format, prompts, templates, EIP-712, wire types | |
| `packages/git-storage/` | Git repository, bundle and PR-input helpers | |
| `packages/chain/` | EVM chain adapter (plus a JSON-file mock chain) and the compiled contract | |
| `demo/` | Demo launcher, client profiles, seed projects, scripted scenarios | [demo/README.md](demo/README.md) |
| `experiments/` | PR-template evaluation harness (`dosr_eval`) and its labeled cases | [experiments/README.md](experiments/README.md) |
| `evaluation/final/` | Cost/latency/gas benchmarks for the final paper, with figures and tables | [evaluation/final/README.md](evaluation/final/README.md) |

## Requirements

- Python 3.10+ and git on `PATH`
- Internet access the first time (Python packages; the demo also downloads Foundry's `anvil` and
  IPFS Kubo into `demo/`)
- For real LLM review: an API key for at least one provider (OpenAI, Anthropic, Google Gemini or Zhipu)

## Quick start

```bash
python demo/setup_env.py          # creates demo/.venv with every DOSR package installed (editable)
cp .env.example .env              # then add OPENAI_API_KEY / ANTHROPIC_API_KEY / ... (optional)
```

`PY` below is the venv's Python: `demo\.venv\Scripts\python.exe` on Windows, `demo/.venv/bin/python`
on macOS/Linux.

| Goal | Command | More |
|---|---|---|
| Run the whole demo (chain + IPFS + attestor + GUI) | `PY demo/demo.py up` (or double-click `demo/start-demo.bat` / `.command`) | [demo/README.md](demo/README.md) |
| Scripted end-to-end demo in the terminal | `PY demo/demo.py run --reset --start-attestor` | [demo/README.md](demo/README.md) |
| Run only the attestor server | `PY demo/demo.py attestor` | [services/attestor/README.md](services/attestor/README.md) |
| Use the client CLI / GUI | `demo/.venv/.../dosr --help`, `dosr gui` | [apps/client/README.md](apps/client/README.md) |
| Re-run the template evaluation | `PY -m dosr_eval ...` (from `experiments/`) | [experiments/README.md](experiments/README.md) |
| Re-run the cost/latency/gas benchmarks | `PY scripts/benchmark_review.py ...` (from `evaluation/final/`) | [evaluation/final/README.md](evaluation/final/README.md) |

## Security model in one paragraph

The client is untrusted. It runs the attestor's checks locally before sending (to catch mistakes
early), but the attestor re-verifies everything itself: the policy hash against the one registered
at genesis, the model against the policy's approved list, the parent against the canonical HEAD on
the chain, and the PR and prompt rebuilt from the bundle. Only then does it call the model with
the contributor's API key (used once, never stored) and sign the decision. The contract trusts only
the policy registered at genesis and signatures from the attestors that policy lists.
