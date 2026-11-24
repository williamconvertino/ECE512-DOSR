# Final evaluation: cost, latency and gas

Benchmarks for the paper's cost and latency evaluation, plus the template-evaluation tables.
`figures/` and `tables/` hold the final outputs; everything else here regenerates them.

| Path | |
|---|---|
| `config.yaml` | models, Template C, list prices, sampling seed and filters, gas-price scenarios, ETH/USD reference |
| `data/workload_manifest.jsonl` | the 35 sampled commits (repository, parent and candidate SHA, size, message) |
| `scripts/prepare_workload.py` | samples real commits by size bucket from six repositories -> `data/` |
| `scripts/benchmark_review.py` | submits each PR through the full DOSR pipeline and records latency, tokens, cost and gas |
| `scripts/benchmark_gas.py` | gas of every DOSRRegistry operation on a fresh local chain |
| `scripts/benchmark_pipeline.py` | per-stage latency and total cost of approved submissions (small / medium / large PRs) |
| `scripts/summarize_template_results.py` | template-evaluation tables from `dosr_eval` results |
| `scripts/generate_tables.py`, `scripts/generate_figures.py` | CSV summaries, LaTeX tables and figures |
| `scripts/harness.py`, `scripts/common.py` | anvil + IPFS + attestor harness; shared helpers |

## How the benchmark works

Every measured submission is a complete DOSR submission through unmodified code: the client
pipeline sends the request over HTTP to the real attestor app (uvicorn on 127.0.0.1), which checks it
against a local anvil chain, rebuilds the prompt from the bundle and calls the model; approved changes
are accepted by DOSRRegistry and published to a local IPFS node, followed by the snapshot-locator
transaction. Token usage and provider latency are captured from outside DOSR (an httpx hook and timers
around the attestor's reviewer). Per PR, a repository is created at the PR's parent tree and
registered once; the chain is reverted to that point before every run, so each submission starts
from the canonical HEAD. Runs are sequential.

## Setup

```bash
python demo/setup_env.py                                   # from the repository root
demo/.venv/Scripts/python -m pip install -e "experiments[dev,figures]" seaborn scipy pyyaml
demo/.venv/Scripts/python demo/demo.py chain install       # anvil
demo/.venv/Scripts/python demo/demo.py ipfs install        # Kubo
```

Provider keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) are read from the repository's `.env` and never
written to results. On Windows, keep the repository at a short path: the benchmark creates Git
repositories under `.cache/`, and Git fails on paths over 260 characters.

## Run

From `evaluation/final/` (`PY` = the venv's Python):

```bash
PY scripts/prepare_workload.py                        # clones the 6 repositories into .cache/, samples 35 commits
PY scripts/benchmark_gas.py                           # no API calls
PY scripts/benchmark_review.py --pilot                # free plumbing check with the mock reviewer
PY scripts/benchmark_review.py --tier primary --paid  # GPT-6-Luna + Claude Haiku 5.5, 3 runs per PR (210 calls)
PY scripts/benchmark_review.py --tier extended --paid # GPT-6.2-Sol + Claude Sonnet 5.5, 1 run per PR (70 calls)
PY scripts/benchmark_pipeline.py                      # Section 2.3 subset (reuses the runs above)
PY scripts/generate_tables.py
PY scripts/generate_figures.py
```

`benchmark_review.py` appends to `results/raw/*.jsonl` after every run and skips completed
`(PR, model, run)` triples, so an interrupted run resumes. Failed provider calls are retried (up to 3
attempts) and logged separately. It stops before exceeding `budget_usd` in `config.yaml`. Do not run two
benchmark processes at once: concurrent runs slow the local stages and distort latency.
`--models`, `--reps` and `--only <pr_id prefix>` restrict a run.

`summarize_template_results.py` reads the template-evaluation runs (`dosr_eval run` output, see
`experiments/README.md`) from `experiments/reports/final-ac-x3/raw/` and its grade sheets.

## Outputs

| File | Content |
|---|---|
| `figures/pr_size_vs_latency.*` | changed lines vs review latency (client -> attestor -> client), all 4 models |
| `figures/pr_size_vs_cost.*` | changed lines vs estimated inference cost (cents, log axis), all 4 models |
| `figures/pipeline_latency_breakdown.*` | median stage latency of approved submissions (EVM stages omitted) |
| `tables/gas_costs.tex` | gas per operation and estimated cost at 1 gwei |
| `tables/pipeline_summary.tex` | total latency, inference cost, transaction cost per model and PR size |
| `tables/template_summary.tex` | accuracy, false rejection and false acceptance for Templates A and C |
| `tables/template_by_category.tex` | Template C approval rate by PR category |

Costs are estimates (provider-reported tokens x list price in `config.yaml`); gas costs are estimates
at assumed gas prices, not fees paid.
