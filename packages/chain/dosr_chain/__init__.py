import os

from .errors import ChainError, StaleParentError
from .mock_chain import MockChain, default_state_path


def connect(kind: str | None = None, *, rpc_url: str | None = None, contract: str | None = None,
            private_key: str | None = None, state_path=None, confirmations: int | None = None):
    env = os.environ
    rpc_url = rpc_url or env.get("DOSR_RPC_URL")
    kind = (kind or env.get("DOSR_CHAIN") or ("evm" if rpc_url else "mock")).lower()
    if kind == "mock":
        return MockChain(state_path or env.get("DOSR_CHAIN_STATE") or None)
    if kind != "evm":
        raise ChainError(f"unknown chain kind {kind!r} (evm or mock)")
    from .evm import EvmChain

    return EvmChain(rpc_url, contract or env.get("DOSR_CONTRACT"),
                    private_key or env.get("DOSR_CHAIN_PRIVATE_KEY"),
                    confirmations if confirmations is not None else int(env.get("DOSR_CONFIRMATIONS", "0") or 0))


def __getattr__(name):
    if name in ("EvmChain", "deploy", "ensure_deployed"):
        from . import evm

        return getattr(evm, name)
    raise AttributeError(name)


__all__ = ["ChainError", "StaleParentError", "MockChain", "EvmChain", "connect", "default_state_path",
           "deploy", "ensure_deployed"]
