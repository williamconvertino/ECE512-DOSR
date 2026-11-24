import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from dosr_git import GitError, GitRepo, pr_inputs
from dosr_protocol.pr_format import PRTemplate, build_pr

POLICY_PATH = ".dosr/policy.json"
PROTECTED_PATHS = frozenset({POLICY_PATH, ".dosr/repo.json"})

MAX_HEADER_BYTES = 1 << 20
OID_HEX_LEN = {"sha1": 40, "sha256": 64}


class BundleError(Exception):
    pass


class MissingPrerequisites(BundleError):

    def __init__(self, missing: list[str]):
        super().__init__(f"attestor does not have prerequisite commit(s) {', '.join(missing)}")
        self.missing = missing


@dataclass
class BundleHeader:
    object_format: str
    prerequisites: list[str]
    heads: dict[str, str]


@dataclass
class Transition:
    pr: dict
    policy_file: bytes | None

    @property
    def changed_files(self) -> list[str]:
        return [f["path"] for f in self.pr["files"]]

    @property
    def touched_paths(self) -> set[str]:
        return {p for f in self.pr["files"] for p in (f["path"], f.get("old_path")) if p}

    @property
    def patch(self) -> str:
        return self.pr["diff"]


def parse_header(data: bytes) -> BundleHeader:
    end = data.find(b"\n\n", 0, MAX_HEADER_BYTES)
    if end < 0:
        raise BundleError("not a Git bundle (no header)")
    lines = data[:end].decode("utf-8", "replace").split("\n")
    if lines[0] not in ("# v2 git bundle", "# v3 git bundle"):
        raise BundleError("not a v2/v3 Git bundle")

    fmt, prereqs, heads = "sha1", [], {}
    for line in lines[1:]:
        if line.startswith("@"):
            key, _, value = line[1:].partition("=")
            if lines[0] == "# v2 git bundle":
                raise BundleError("v2 bundles cannot carry capabilities")
            if key == "object-format":
                fmt = value
            elif key == "filter":
                raise BundleError("filtered (partial) bundles are not accepted")
        elif line.startswith("-"):
            prereqs.append(line[1:].split(" ", 1)[0])
        else:
            oid, _, ref = line.partition(" ")
            heads[oid] = ref

    if fmt not in OID_HEX_LEN:
        raise BundleError(f"unsupported object format {fmt!r}")
    for oid in [*prereqs, *heads]:
        if len(oid) != OID_HEX_LEN[fmt] or any(c not in "0123456789abcdef" for c in oid):
            raise BundleError(f"malformed object id {oid!r} in bundle header")
    return BundleHeader(fmt, prereqs, heads)


def _git(git_dir: Path | None, *args: str, timeout: float, check: bool = True,
         input: bytes | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    cmd = ["git"] + ([f"--git-dir={git_dir}"] if git_dir else []) + list(args)
    try:
        proc = subprocess.run(cmd, env=env, input=input, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BundleError(f"git {args[0]} timed out")
    if check and proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise BundleError(f"git {args[0]} failed: {msg[-1] if msg else proc.returncode}")
    return proc


class _SandboxedGitRepo(GitRepo):

    def __init__(self, git_dir: Path, timeout: float):
        super().__init__(git_dir)
        self.timeout = timeout

    def run_bytes(self, *args: str, check: bool = True, input: bytes | None = None) -> bytes:
        proc = _git(self.path, "-c", "core.quotepath=off", *args, timeout=self.timeout, check=False, input=input)
        if check and proc.returncode != 0:
            raise GitError(list(args), proc.returncode, proc.stderr.decode("utf-8", "replace"))
        return proc.stdout


def _has_commit(git_dir: Path, oid: str, timeout: float) -> bool:
    return _git(git_dir, "cat-file", "-e", f"{oid}^{{commit}}", timeout=timeout, check=False).returncode == 0


class ObjectCache:

    def __init__(self, root: Path, timeout: float):
        self.root = Path(root)
        self.timeout = timeout
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def path(self, repo_id: str) -> Path:
        return self.root / f"{repo_id.removeprefix('0x').lower()}.git"

    def has_commit(self, repo_id: str, oid: str) -> bool:
        path = self.path(repo_id)
        return path.is_dir() and _has_commit(path, oid, self.timeout)

    def _lock(self, repo_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(repo_id, threading.Lock())

    def add(self, repo_id: str, object_format: str, source: Path, oid: str) -> None:
        path = self.path(repo_id)
        with self._lock(repo_id):
            if not path.is_dir():
                path.parent.mkdir(parents=True, exist_ok=True)
                _git(None, "init", "-q", "--bare", f"--object-format={object_format}", str(path),
                     timeout=self.timeout)
            _git(source, "update-ref", "refs/dosr/candidate", oid, timeout=self.timeout)
            _git(path, "fetch", "-q", "--no-tags", str(source), f"refs/dosr/candidate:refs/dosr/seen/{oid}",
                 timeout=self.timeout)


def open_transition(
    cache: ObjectCache,
    repo_id: str,
    object_format: str,
    bundle: bytes,
    parent: str,
    candidate: str,
    template: PRTemplate,
    repo_name: str | None,
) -> Transition:
    t = cache.timeout
    header = parse_header(bundle)
    if header.object_format != object_format:
        raise BundleError(f"bundle uses {header.object_format} objects, the policy requires {object_format}")
    if candidate not in header.heads:
        raise BundleError("bundle does not contain the candidate commit")

    with tempfile.TemporaryDirectory(prefix="dosr-attestor-") as tmp:
        work = Path(tmp) / "work.git"
        _git(None, "init", "-q", "--bare", f"--object-format={object_format}", str(work), timeout=t)
        cached = cache.path(repo_id) / "objects"
        if cached.is_dir():
            (work / "objects" / "info" / "alternates").write_text(f"{cached.resolve()}\n", newline="\n")

        missing = [p for p in header.prerequisites if not _has_commit(work, p, t)]
        if missing:
            raise MissingPrerequisites(missing)

        bundle_path = Path(tmp) / "change.bundle"
        bundle_path.write_bytes(bundle)
        _git(work, "bundle", "unbundle", str(bundle_path), timeout=t)
        _git(work, "rev-list", "--objects", "--quiet", candidate, timeout=t)

        if not _has_commit(work, parent, t):
            raise BundleError(f"bundle does not contain the declared parent commit {parent}")
        if _git(work, "merge-base", "--is-ancestor", parent, candidate, timeout=t, check=False).returncode != 0:
            raise BundleError(f"candidate does not descend from the declared parent {parent}")

        try:
            pr = build_pr(
                template=template,
                repo={"name": repo_name, "repo_id": repo_id},
                parent_git_oid=f"{object_format}:{parent}",
                candidate_git_oid=f"{object_format}:{candidate}",
                **pr_inputs(_SandboxedGitRepo(work, t), template, parent, candidate),
            )
        except GitError as e:
            raise BundleError(f"cannot read the transition: {e.stderr or e}")
        shown = _git(work, "cat-file", "blob", f"{candidate}:{POLICY_PATH}", timeout=t, check=False)

        cache.add(repo_id, object_format, work, candidate)

    return Transition(pr=pr, policy_file=shown.stdout if shown.returncode == 0 else None)
