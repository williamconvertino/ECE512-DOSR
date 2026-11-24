import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dosr_chain import MockChain, connect

DEV_PRIVATE_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"


@dataclass(frozen=True)
class Settings:
    private_key: str = DEV_PRIVATE_KEY
    key_id: str = "local-dev-attestor-1"
    chain_state: Path | None = None
    chain_kind: str | None = None
    chain_rpc_url: str | None = None
    chain_contract: str | None = None
    cache_dir: Path = Path.home() / ".dosr" / "attestor-cache"
    max_bundle_bytes: int = 50 * 1024 * 1024
    git_timeout_seconds: float = 30.0
    attestation_ttl_seconds: int = 3600
    providers_file: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        default = cls()
        return cls(
            private_key=env.get("ATTESTOR_PRIVATE_KEY", default.private_key),
            key_id=env.get("ATTESTOR_KEY_ID", default.key_id),
            chain_state=Path(env["DOSR_CHAIN_STATE"]) if env.get("DOSR_CHAIN_STATE") else None,
            chain_kind=env.get("DOSR_CHAIN") or None,
            chain_rpc_url=env.get("DOSR_RPC_URL") or None,
            chain_contract=env.get("DOSR_CONTRACT") or None,
            cache_dir=Path(env.get("ATTESTOR_CACHE_DIR", default.cache_dir)),
            max_bundle_bytes=int(env.get("ATTESTOR_MAX_BUNDLE_BYTES", default.max_bundle_bytes)),
            providers_file=Path(env["ATTESTOR_PROVIDERS_FILE"]) if env.get("ATTESTOR_PROVIDERS_FILE") else None,
        )

    def chain(self):
        if self.chain_state is not None:
            return MockChain(self.chain_state)
        return _connect(self.chain_kind, self.chain_rpc_url, self.chain_contract)


@lru_cache(maxsize=8)
def _connect(kind, rpc_url, contract):
    return connect(kind, rpc_url=rpc_url, contract=contract)
