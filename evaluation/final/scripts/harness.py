import contextlib
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import common
from common import CACHE, ROOT

ANVIL = ROOT / "demo" / ".foundry" / "bin" / "anvil.exe"
IPFS = ROOT / "demo" / ".ipfs" / "bin" / "ipfs.exe"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Kubo:

    def __init__(self, repo: Path = CACHE / "ipfs"):
        self.repo = repo
        self.proc = None
        self.api = None

    def __enter__(self):
        env = {**os.environ, "IPFS_PATH": str(self.repo)}
        if not (self.repo / "config").exists():
            self.repo.mkdir(parents=True, exist_ok=True)
            subprocess.run([str(IPFS), "init", "--empty-repo"], env=env, check=True, stdout=subprocess.DEVNULL)
        api, gw = free_port(), free_port()
        for setting in (("Addresses.API", f"/ip4/127.0.0.1/tcp/{api}"),
                        ("Addresses.Gateway", f"/ip4/127.0.0.1/tcp/{gw}"),
                        ("--json", "Addresses.Swarm", "[]")):
            subprocess.run([str(IPFS), "config", *setting], env=env, check=True)
        self.log = open(self.repo / "daemon.log", "ab")
        self.proc = subprocess.Popen([str(IPFS), "daemon", "--offline"], env=env, stdout=self.log,
                                     stderr=subprocess.STDOUT)
        self.api = f"http://127.0.0.1:{api}"
        from dosr_client.ipfs import IpfsStore

        store = IpfsStore(self.api)
        for _ in range(240):
            if store.available(max_age=0):
                return self
            if self.proc.poll() is not None:
                raise RuntimeError(f"ipfs daemon exited ({self.proc.returncode}); see {self.repo / 'daemon.log'}")
            time.sleep(0.25)
        raise RuntimeError("ipfs daemon did not come up")

    def version(self) -> str:
        return subprocess.run([str(IPFS), "--version"], capture_output=True, text=True).stdout.strip()

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()


class TimedStore:

    def __init__(self, ipfs_store):
        self.ipfs = ipfs_store
        self.adds = []

    def writer(self):
        return self

    kind = "ipfs"

    def add(self, data: bytes, name: str = "blob") -> str:
        t = time.perf_counter()
        cid = self.ipfs.add(data, name)
        self.adds.append({"name": name, "bytes": len(data), "ms": (time.perf_counter() - t) * 1000})
        return cid

    def cat(self, ident, timeout=None):
        return self.ipfs.cat(ident, timeout)


class Chain:

    def __enter__(self):
        from dosr_chain.dev import Anvil

        self.node = Anvil(ANVIL).__enter__()
        self.rpc_url, self.contract = self.node.rpc_url, self.node.contract
        return self

    def chain(self, account_index: int = 2):
        from dosr_chain.dev import dev_key

        return self.node.chain(private_key=dev_key(account_index))

    def rpc(self, method, *params):
        from dosr_chain.evm import Rpc

        return Rpc(self.rpc_url, timeout=30)(method, *params)

    def snapshot(self) -> str:
        return self.rpc("evm_snapshot")

    def revert(self, snap: str) -> None:
        if not self.rpc("evm_revert", snap):
            raise RuntimeError("evm_revert failed")

    def __exit__(self, *exc):
        self.node.__exit__(*exc)


class TimedChain:

    def __init__(self, inner):
        self._inner = inner
        self.snapshot_calls = []

    def publish_snapshot(self, repo_id, head, cid):
        t = time.perf_counter()
        out = self._inner.publish_snapshot(repo_id, head, cid)
        self.snapshot_calls.append({"ms": (time.perf_counter() - t) * 1000, "gas_used": out.get("gas_used"),
                                    "cid_len": len(cid)})
        return out

    def __getattr__(self, name):
        return getattr(self._inner, name)


class Probe:

    def __init__(self):
        self.reset()

    def reset(self):
        self.t = {}
        self.usage = None
        self.provider_status = None
        self.provider_error = None
        self.raw_len = None


def normalize_usage(u) -> dict | None:
    if not isinstance(u, dict):
        return None
    if "input_tokens" in u or "output_tokens" in u:
        prompt = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        return {"prompt_tokens": prompt, "completion_tokens": u.get("output_tokens") or 0,
                "cached_tokens": u.get("cache_read_input_tokens") or 0,
                "reasoning_tokens": (u.get("output_tokens_details") or {}).get("thinking_tokens"), "raw": u}
    return {"prompt_tokens": u.get("prompt_tokens") or 0, "completion_tokens": u.get("completion_tokens") or 0,
            "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
            "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"), "raw": u}


def build_attestor(probe: Probe, cache_dir: Path, rpc_url: str, contract: str, providers_file: Path):
    import httpx

    from attestor import reviewers as rv
    from attestor.config import Settings
    from attestor.review import Attestor

    def hook(response: httpx.Response):
        response.read()
        probe.provider_status = response.status_code
        try:
            probe.usage = normalize_usage(response.json().get("usage"))
        except Exception:
            probe.usage = None

    class TimedReviewer:
        def __init__(self, inner):
            self.inner = inner
            self.needs_key = inner.needs_key

        def complete(self, prompt, model, api_key=None):
            probe.t["infer_start"] = time.perf_counter()
            try:
                raw = self.inner.complete(prompt, model, api_key)
                probe.raw_len = len(raw)
                return raw
            except Exception as e:
                probe.provider_error = str(e)[:500]
                raise
            finally:
                probe.t["infer_end"] = time.perf_counter()

    reviewers = {"mock": TimedReviewer(rv.MockReviewer())}
    for name, provider in rv.load_providers(providers_file).items():
        http = httpx.Client(timeout=300.0, event_hooks={"response": [hook]})
        reviewers[name] = TimedReviewer(rv.BACKENDS[provider.api](name, provider, http=http))

    class TimedAttestor(Attestor):
        def review(self, *a, **k):
            probe.t["review_start"] = time.perf_counter()
            try:
                return super().review(*a, **k)
            finally:
                probe.t["review_end"] = time.perf_counter()

    settings = Settings(chain_kind="evm", chain_rpc_url=rpc_url, chain_contract=contract, cache_dir=cache_dir,
                        providers_file=providers_file, max_bundle_bytes=200 * 1024 * 1024)
    return TimedAttestor(settings, reviewers=reviewers)


class AttestorServer:

    def __init__(self, attestor):
        self.attestor = attestor

    def __enter__(self):
        import uvicorn

        from attestor.main import app, get_attestor

        app.dependency_overrides[get_attestor] = lambda: self.attestor
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("attestor did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)
        from attestor.main import app

        app.dependency_overrides.clear()


@contextlib.contextmanager
def infrastructure(providers_file: Path, work: Path, ipfs_repo: Path = CACHE / "ipfs"):
    probe = Probe()
    with Chain() as chain, Kubo(ipfs_repo) as kubo:
        att = build_attestor(probe, work / "attestor-cache", chain.rpc_url, chain.contract, providers_file)
        with AttestorServer(att) as server:
            yield {"chain": chain, "kubo": kubo, "attestor": att, "server": server, "probe": probe}
