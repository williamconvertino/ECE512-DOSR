# DOSR attestor (server)

The attestor reviews DOSR pull requests. For each request it:

1. checks the request against the policy (policy hash, approved model and version, template,
   decision schema, object format, bundle checksum) and against the chain (repository registered,
   policy hash matches genesis, parent is the canonical HEAD);
2. unpacks the Git bundle in a sandboxed repository, rebuilds the PR from it and enforces the policy
   limits (changed files, patch size, protected `.dosr/` files);
3. builds the review prompt itself and rejects the request if the client's prompt differs;
4. calls the approved model with the contributor's API key (sent in `X-DOSR-Provider-Key`, used once,
   never stored);
5. parses the decision (unparseable output is a rejection) and signs an EIP-712 attestation.

Every request that reaches step 1 gets a signed response, rejections included. Deterministic checks
run before the model, so invalid requests never cost a model call.

## Setup

```bash
python demo/setup_env.py                       # from the repository root; or:
pip install -r services/attestor/requirements.txt
pip install -e packages/protocol -e packages/git-storage -e packages/chain
```

## Run

Easiest, with the demo's chain settings (starts nothing else):

```bash
PY demo/demo.py chain          # terminal 1: local EVM chain (anvil) with DOSRRegistry deployed
PY demo/demo.py attestor       # terminal 2: attestor on http://127.0.0.1:8080
```

Or directly with uvicorn, from `services/attestor/`:

```bash
cd services/attestor
export DOSR_RPC_URL=http://127.0.0.1:8545
export DOSR_CONTRACT=0x5FbDB2315678afecb367f032d93F642f64180aa3
export ATTESTOR_PROVIDERS_FILE=../../experiments/providers.json   # needed for GPT-5/6, o-series and Claude 5.x models
python -m uvicorn attestor.main:app --port 8080
```

Interactive API docs: `http://127.0.0.1:8080/docs`.

## Configuration (environment variables)

| Variable | Meaning | Default |
|---|---|---|
| `DOSR_CHAIN` | `evm` or `mock` | `evm` if `DOSR_RPC_URL` is set, else `mock` |
| `DOSR_RPC_URL` | EVM JSON-RPC endpoint | |
| `DOSR_CONTRACT` | DOSRRegistry address | dev deployment address |
| `DOSR_CHAIN_STATE` | mock chain JSON file (forces the mock chain; must be the client's file) | |
| `ATTESTOR_PRIVATE_KEY` | secp256k1 signing key | public anvil test key #1 (**development only**) |
| `ATTESTOR_KEY_ID` | key id that repository policies list | `local-dev-attestor-1` |
| `ATTESTOR_CACHE_DIR` | per-repository Git object cache (lets clients send thin bundles) | `~/.dosr/attestor-cache` |
| `ATTESTOR_MAX_BUNDLE_BYTES` | largest accepted bundle | 50 MB |
| `ATTESTOR_PROVIDERS_FILE` | JSON file adding or overriding model providers / per-model request fields | built-in providers only |

A repository only accepts attestations from attestors its policy lists. The default policies list
`local-dev-attestor-1` at `0x70997970C51812dc3A010C7d01b50e0d17dc79C8` (the default key). To run your
own key, set `ATTESTOR_PRIVATE_KEY` / `ATTESTOR_KEY_ID` and create repositories with
`dosr init ... --attestor <KEY_ID>=<ADDRESS>`.

**Model providers.** Built in: OpenAI, Anthropic, Google Gemini and Zhipu. Any other
OpenAI-compatible provider can be added in the providers file, e.g.
`{"deepseek": {"url": "https://api.deepseek.com/chat/completions"}}`. Reasoning models need the
per-model fields in `experiments/providers.json` (no `temperature`, larger output limit,
`reasoning_effort`); this is the file the experiments used.

## API

| Method and path | Purpose |
|---|---|
| `GET /v1/health` | status and protocol version |
| `GET /v1/keys` | the attestor's signing key id, scheme and address |
| `GET /v1/chain` | which chain and contract this attestor reads |
| `GET /v1/repos/{repo_id}/commits/{git_oid}` | whether the attestor already holds a commit (thin bundles) |
| `POST /v1/reviews` | multipart: `request` (JSON), `policy` (JSON), `bundle` (Git bundle), optional `prompt`; header `X-DOSR-Provider-Key` |

Errors that are not review decisions return HTTP errors without a signature: 400 (malformed input or
missing provider key), 409 (thin bundle whose parent the attestor does not have: resend a full
bundle), 413 (too large), 422 (schema), 502 (model provider unreachable).
