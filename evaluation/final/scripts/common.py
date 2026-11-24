import json
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FINAL = Path(__file__).resolve().parents[1]
DATA, RESULTS, FIGURES, TABLES = (FINAL / d for d in ("data", "results", "figures", "tables"))
RAW = RESULTS / "raw"
CACHE = FINAL / ".cache"
REPOS = CACHE / "repos"

os.environ["GIT_CONFIG_NOSYSTEM"] = "1"

for p in ("services/attestor", "experiments"):
    if str(ROOT / p) not in sys.path:
        sys.path.insert(0, str(ROOT / p))


def load_config() -> dict:
    import yaml

    return yaml.safe_load((FINAL / "config.yaml").read_text("utf-8"))


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def cost_usd(model: str, usage: dict | None, pricing: dict) -> float | None:
    p = pricing.get(model)
    if not p or not usage:
        return None
    prompt = usage.get("prompt_tokens") or 0
    cached = usage.get("cached_tokens") or 0
    out = usage.get("completion_tokens") or 0
    rates = p
    lc = p.get("long_context")
    if lc and prompt > lc["above_prompt_tokens"]:
        rates = {**p, **lc}
    cached_rate = rates.get("cached_input", rates["input"])
    return ((prompt - cached) * rates["input"] + cached * cached_rate + out * rates["output"]) / 1e6


def short_model(spec: str) -> str:
    return spec.split("/", 1)[1].split("@", 1)[0]


MODEL_LABELS = {
    "openai/gpt-6-luna@latest": "GPT-6-Luna",
    "anthropic/claude-haiku-5-5@latest": "Claude Haiku 5.5",
    "openai/gpt-6.2-sol@latest": "GPT-6.2-Sol",
    "anthropic/claude-sonnet-5-5@latest": "Claude Sonnet 5.5",
}


def environment() -> dict:
    def ver(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip().splitlines()[0]
        except Exception:
            return None

    try:
        commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                                text=True).stdout.strip()
    except Exception:
        commit = None
    cpu = None
    if platform.system() == "Windows":
        try:
            import winreg

            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            cpu = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except Exception:
            pass
    mem = None
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong), ("c", ctypes.c_ulonglong),
                        ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong), ("f", ctypes.c_ulonglong)]

        ms = MS()
        ms.l = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
        mem = round(ms.total / 2**30, 1)
    except Exception:
        pass
    return {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu": cpu or platform.processor(),
        "logical_cpus": os.cpu_count(),
        "ram_gib": mem,
        "python": sys.version.split()[0],
        "git": ver(["git", "--version"]),
        "anvil": ver([str(ROOT / "demo/.foundry/bin/anvil.exe"), "--version"]),
        "ipfs": ver([str(ROOT / "demo/.ipfs/bin/ipfs.exe"), "--version"]),
        "dosr_commit": commit,
    }


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text("utf-8").splitlines() if l.strip()]
