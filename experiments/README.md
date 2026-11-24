# PR template evaluation (`dosr_eval`)

Measures how well a **PR template x model** pair reviews pull requests. Each labeled case is turned
into a real Git parent -> candidate transition, and the prompt is built with the same code the
attestor uses (`dosr_git.pr_inputs` -> `pr_format.build_pr` -> `render_markdown` ->
`decision.build_prompt`). The model is called through the attestor's own reviewer classes, so the
request body is what production sends. Unparseable output counts as a rejection, as in the attestor.

## Setup

```bash
python demo/setup_env.py                                     # from the repository root
demo/.venv/Scripts/python -m pip install -e "experiments[dev,figures]"
cd experiments
cp pricing.example.json pricing.json                         # list prices used for cost columns and --budget
```

API keys come from `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` or `ZHIPU_API_KEY`, in the
environment or the repository's `.env`. Below, `PY` is the venv's Python, run from `experiments/`.

**Nothing costs money by default**: the default model is the mock reviewer, and a paid model only
prints the plan and a cost estimate unless `--paid` is given. `--budget X` stops before a call that
could push the total past X.

## Contents

| Path | |
|---|---|
| `dosr_eval/` | the harness (cases, prompts, runner, metrics, report/paper outputs, importers) |
| `cases/final-v1/` | the final suite: 122 labeled cases (118 reported; see below) |
| `cases/pilot-v1/` | the 20-case pilot used to select templates |
| `templates/exp-evidence-v1.toml` | Template **B**: Template A plus "cite `path:line - code - why` for every issue" |
| `templates/exp-blocking-only-v1.toml` | Template **C**: Template A's rules, but only problems the change makes worse are blocking |
| `providers.json` | per-model request fields for reasoning models (GPT-5/6, o-series, Claude 5.x) |
| `pricing.example.json` | list prices per million tokens |

Template **A** is the built-in `security-sensitive-review-v2` (`packages/protocol/dosr_protocol/templates/`).

## Commands

```bash
PY -m dosr_eval --cases cases/final-v1 list                      # cases and labels
PY -m dosr_eval --cases cases/final-v1 prompt <case-id> --template exp-blocking-only-v1   # exact prompt, no call
PY -m dosr_eval models                                           # providers and whether a key is set

# The final evaluation: Templates A and C, 3 runs each, one model per run
PY -m dosr_eval --cases cases/final-v1 run \
   --template security-sensitive-review-v2 --template exp-blocking-only-v1 \
   --model openai/gpt-6-luna@latest --reps 3 \
   --providers-file providers.json --pricing pricing.json --budget 2 --paid --label final-ac-x3-luna
# (repeat with --model anthropic/claude-haiku-5-5@latest)

# The template-selection pilot: Templates A, B and C on gpt-6-luna
PY -m dosr_eval --cases cases/pilot-v1 run --template security-sensitive-review-v2 \
   --template exp-evidence-v1 --template exp-blocking-only-v1 --model openai/gpt-6-luna@latest \
   --reps 3 --providers-file providers.json --paid --label pilot-abc-x3

# Call-site context variant (adds the changed functions' call sites to the prompt)
PY -m dosr_eval --cases cases/final-v1 run ... --context callers

PY -m dosr_eval report results/<run>.jsonl --exclude <case-id> ...          # accuracy, false reject/accept, ...
PY -m dosr_eval grade results/<run>.jsonl                                  # CSV to hand-grade diagnosis (y/n)
PY -m dosr_eval paper results/<run1>.jsonl results/<run2>.jsonl \
   --grades <run1>.grades.csv --grades <run2>.grades.csv --exclude <case-id> --currency USD
PY -m dosr_eval spend                                                      # API spend across runs
```

`run --dry-run` builds every prompt and prints the call count and a worst-case cost without calling
anything. Each run writes `results/<run_id>.jsonl` (one row per model call) and `.meta.json`.
`paper` writes LaTeX tables and figures by case group to `reports/<run>/`, including the
misleading-description framings and simulated multi-reviewer policies.

## Final suite (`cases/final-v1`)

| Group | Source | Cases | Expected |
|---|---|---|---|
| Valid security fix | SEVRA upstream fixes, 20 vulnerabilities | 20 | approve |
| Security fix reversion | the same fixes reverted (neutral `Update <file>` message) | 20 | reject |
| Reversion with misleading description | the reverts with SEVRA's `prior_approval_signal`, `fake_bug_fix` and `review_bias` PR texts | 60 | reject |
| Valid bug fix | SWE-bench Verified upstream fixes, one issue per repository | 11 | approve |
| Incorrect patch | failing official-leaderboard patches for the same issues (`broke_other_code` or `did_not_fix`) | 11 | reject |

Two pairs are excluded from all reported numbers (`--exclude`): `final-swe-astropy-astropy-8872-*` and
`final-swe-pylint-dev-pylint-4604-*`. Their official failures cannot show the patch is wrong (the
astropy test module fails to collect; the pylint tests import a helper only the upstream fix adds).

New cases can be imported with `import-sevra` (fix, revert and misleading reverts for a SEVRA
vulnerability) and `import-swebench` (gold fix and a failing leaderboard patch for an issue).

## Metrics

| Metric | Meaning |
|---|---|
| accuracy | correct verdicts / scored calls |
| false rejection | share of should-approve calls that rejected |
| false acceptance (miss) | share of should-reject calls that approved |
| parse fail | output the attestor could not parse (counted as a rejection) |
| flip | cases whose verdict differs across runs |
| diagnosis | correct rejections whose reason names the case's target issue (graded by hand) |

Provider errors are recorded but not scored.
