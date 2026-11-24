import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
from dosr_protocol.models import key_envs

ROOT = Path(__file__).resolve().parents[2]
ATTESTOR_DIR = ROOT / "services" / "attestor"

FREE_PROVIDERS = {"mock"}


def attestor_reviewers():
    if str(ATTESTOR_DIR) not in sys.path:
        sys.path.insert(0, str(ATTESTOR_DIR))
    from attestor import reviewers

    return reviewers


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    version: str

    @classmethod
    def parse(cls, spec: str) -> "ModelSpec":
        provider, sep, rest = spec.partition("/")
        model, sep2, version = rest.partition("@")
        if not (sep and sep2 and provider and model and version):
            raise ValueError(f"model must look like provider/model@version, got {spec!r}")
        return cls(provider, model, version)

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}@{self.version}"

    def as_review(self):
        return SimpleNamespace(provider=self.provider, model=self.model, model_version=self.version)

    @property
    def paid(self) -> bool:
        return self.provider not in FREE_PROVIDERS


def normalize_usage(usage) -> dict | None:
    if not isinstance(usage, dict):
        return None
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        return usage
    if "input_tokens" in usage or "output_tokens" in usage:
        prompt = sum(usage.get(k) or 0 for k in
                     ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        return {"prompt_tokens": prompt, "completion_tokens": usage.get("output_tokens") or 0, "raw": usage}
    return usage


class UsageRecorder:

    def __init__(self):
        self.last: dict | None = None

    def reset(self) -> None:
        self.last = None

    def __call__(self, response: httpx.Response) -> None:
        response.read()
        try:
            usage = response.json().get("usage")
        except (ValueError, AttributeError):
            usage = None
        self.last = normalize_usage(usage)


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def api_key_env(provider: str) -> str:
    envs = key_envs(provider)
    return envs[0] if envs else ""


def find_key(provider: str) -> str | None:
    return next((os.environ[e] for e in key_envs(provider) if os.environ.get(e)), None)


class Backend:
    def __init__(self, spec: ModelSpec, providers_file: Path | None = None, timeout: float = 120.0,
                 http: httpx.Client | None = None):
        rv = attestor_reviewers()
        self.spec = spec
        self.usage = UsageRecorder()
        if spec.provider == "mock":
            self.reviewer = rv.MockReviewer()
        else:
            providers = rv.load_providers(providers_file)
            if spec.provider not in providers:
                raise ValueError(f"unknown provider {spec.provider!r} (known: mock, {', '.join(sorted(providers))})")
            provider = providers[spec.provider]
            if provider.api not in rv.BACKENDS:
                raise ValueError(f"provider {spec.provider!r}: unknown api {provider.api!r}")
            if http is None:
                http = httpx.Client(timeout=timeout)
            hooks = http.event_hooks
            http.event_hooks = {**hooks, "response": [*hooks.get("response", []), self.usage]}
            self.reviewer = rv.BACKENDS[provider.api](spec.provider, provider, http=http)
        self.needs_key = self.reviewer.needs_key
        self.key = find_key(spec.provider) if self.needs_key else None

    def complete(self, prompt: str) -> tuple[str, dict | None]:
        self.usage.reset()
        raw = self.reviewer.complete(prompt, self.spec.as_review(), self.key)
        return raw, self.usage.last
