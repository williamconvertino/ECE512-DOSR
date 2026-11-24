import json
import re
import string
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .canonical import canonical_json, sha256_0x

try:
    import tomllib as _toml
except ModuleNotFoundError:
    import tomli as _toml

TEMPLATE_FORMAT = "dosr-pr-template-v1"
TEMPLATE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_TEXT = 20_000
MAX_GLOBS = 50
MAX_SECTIONS = 40

GLOBAL_VARIABLES = {
    "title", "repo_name", "repo_id", "repo_id_short", "parent", "candidate", "template_id",
    "template_hash", "format", "pr_hash", "changed_files", "additions", "deletions",
    "patch_bytes", "commit_count",
}
ITEM_VARIABLES = {
    "commits": {"oid", "oid_short", "subject", "author"},
    "changed_files": {"path", "status", "additions", "deletions"},
    "instructions": {"item"},
    "full_files": {"path"},
    "context_files": {"path"},
}
VARIABLES = {"*": sorted(GLOBAL_VARIABLES), **{k: sorted(v) for k, v in ITEM_VARIABLES.items()}}

SECTION_TYPES: dict[str, dict] = {
    "text": {"body": None, "heading": None},
    "description": {"heading": "Description"},
    "commits": {"heading": "Commits", "item": "- `{oid_short}` {subject} — {author}"},
    "changed_files": {"heading": "Changed files", "item": "- `{status}` {path} (+{additions}/-{deletions})"},
    "instructions": {"heading": "Review instructions", "item": "- [ ] {item}", "include_checklist": True},
    "diff": {"heading": "Diff", "fence": "diff"},
    "full_files": {"heading": "Full changed files", "file_heading": "### {path}", "fence": ""},
    "context_files": {"heading": "Context files", "file_heading": "### {path}", "fence": ""},
}

HEADER_BODY = """\
# {title}

- Repository: `{repo_name}` (`{repo_id_short}…`)
- Parent: `{parent}`
- Candidate: `{candidate}`
- Template: `{template_id}` · Format: `{format}`
- Stats: {changed_files} file(s), +{additions} / -{deletions}, {patch_bytes} patch bytes
- PR hash: `{pr_hash}`"""

DEFAULT_SECTIONS: tuple[dict, ...] = (
    {"type": "text", "body": HEADER_BODY},
    {"type": "description"},
    {"type": "commits"},
    {"type": "changed_files"},
    {"type": "instructions"},
    {"type": "diff"},
    {"type": "full_files"},
    {"type": "context_files"},
)


class TemplateError(ValueError):
    pass


@dataclass(frozen=True)
class PRTemplate:
    id: str
    description: str
    diff_context_lines: int
    include_full_files: bool
    context_globs: tuple[str, ...]
    max_file_bytes: int
    instructions: str
    checklist: tuple[str, ...] = ()
    sections: tuple[dict, ...] = DEFAULT_SECTIONS
    definition: dict = field(default_factory=dict, compare=False, repr=False)
    builtin: bool = False

    @property
    def hash(self) -> str:
        return template_hash(self.definition)


def template_hash(definition: dict) -> str:
    return sha256_0x(canonical_json(definition))


def _fail(where: str, msg: str):
    raise TemplateError(f"{where}: {msg}")


def _check_keys(obj, allowed: set, required: set, where: str) -> None:
    if not isinstance(obj, dict):
        _fail(where, "must be a table/object")
    missing, unknown = required - obj.keys(), obj.keys() - allowed
    if missing:
        _fail(where, f"missing {', '.join(sorted(missing))}")
    if unknown:
        _fail(where, f"unknown key(s) {', '.join(sorted(unknown))} (allowed: {', '.join(sorted(allowed))})")


def _check_text(value, where: str, *, allow_empty=False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or len(value) > MAX_TEXT:
        _fail(where, f"must be a {'' if allow_empty else 'non-empty '}string of at most {MAX_TEXT} characters")
    return value


def _check_int(value, lo: int, hi: int, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        _fail(where, f"must be an integer from {lo} to {hi}")
    return value


def _check_format_string(text: str, allowed: set, where: str) -> None:
    try:
        parts = list(string.Formatter().parse(text))
    except ValueError as e:
        _fail(where, f"bad format string ({e}); write {{{{ and }}}} for literal braces")
    for _, name, spec, conversion in parts:
        if name is None:
            continue
        if name not in allowed:
            _fail(where, f"unknown variable {{{name}}} (available: {', '.join(sorted(allowed))})")
        if spec or conversion:
            _fail(where, f"{{{name}}}: format specs and conversions are not supported")


def check_globs(globs, where: str) -> tuple[str, ...]:
    if not isinstance(globs, list) or len(globs) > MAX_GLOBS or not all(
        isinstance(g, str) and g.strip() and not g.startswith(("/", "../")) and ".." not in g.split("/")
        for g in globs
    ):
        _fail(where, f"must be a list of at most {MAX_GLOBS} relative paths/globs inside the repository")
    return tuple(globs)


def _check_section(sec, i: int) -> dict:
    where = f"sections[{i}]"
    if not isinstance(sec, dict) or sec.get("type") not in SECTION_TYPES:
        _fail(where, f"`type` must be one of {', '.join(SECTION_TYPES)}")
    kind = sec["type"]
    options = SECTION_TYPES[kind]
    _check_keys(sec, {"type", *options}, {"type", *(["body"] if kind == "text" else [])}, f"{where} ({kind})")
    item_vars = GLOBAL_VARIABLES | ITEM_VARIABLES.get(kind, set())
    for key, value in sec.items():
        if key == "type":
            continue
        if key == "include_checklist":
            if not isinstance(value, bool):
                _fail(f"{where}.{key}", "must be true or false")
            continue
        _check_text(value, f"{where}.{key}", allow_empty=key in ("fence", "heading"))
        if key == "fence" and not re.fullmatch(r"[A-Za-z0-9_+.-]*", value):
            _fail(f"{where}.fence", "must be a code-fence language like 'diff', 'python' or empty")
        if key in ("body", "heading"):
            _check_format_string(value, GLOBAL_VARIABLES, f"{where}.{key}")
        if key in ("item", "file_heading"):
            _check_format_string(value, item_vars, f"{where}.{key}")
    return dict(sec)


def parse_template(definition: dict, *, where: str = "template", builtin: bool = False) -> PRTemplate:
    try:
        json.dumps(definition, allow_nan=False)
    except (TypeError, ValueError):
        _fail(where, "may only contain strings, numbers, booleans, lists and tables (no dates)")
    _check_keys(definition, {"format", "id", "description", "context", "review", "sections"},
                {"format", "id", "description", "context", "review"}, where)
    if definition["format"] != TEMPLATE_FORMAT:
        _fail(f"{where}.format", f"must be {TEMPLATE_FORMAT!r}")
    tid = definition["id"]
    if not isinstance(tid, str) or not TEMPLATE_ID.match(tid):
        _fail(f"{where}.id", "must be 1-64 chars of lowercase letters, digits, '.', '_', '-' (e.g. 'team-review-v1')")
    description = _check_text(definition["description"], f"{where}.description")
    ctx, rv = definition["context"], definition["review"]
    _check_keys(ctx, {"diff_context_lines", "include_full_files", "max_file_bytes", "files"},
                {"diff_context_lines", "include_full_files", "max_file_bytes"}, f"{where}.context")
    if not isinstance(ctx["include_full_files"], bool):
        _fail(f"{where}.context.include_full_files", "must be true or false")
    _check_keys(rv, {"instructions", "checklist"}, {"instructions"}, f"{where}.review")
    checklist = rv.get("checklist", [])
    if not isinstance(checklist, list) or len(checklist) > 50:
        _fail(f"{where}.review.checklist", "must be a list of at most 50 strings")
    for i, item in enumerate(checklist):
        _check_text(item, f"{where}.review.checklist[{i}]")
    sections = definition.get("sections")
    if sections is None:
        parsed_sections = DEFAULT_SECTIONS
    else:
        if not isinstance(sections, list) or not 1 <= len(sections) <= MAX_SECTIONS:
            _fail(f"{where}.sections", f"must be a list of 1..{MAX_SECTIONS} sections")
        parsed_sections = tuple(_check_section(s, i) for i, s in enumerate(sections))
    return PRTemplate(
        id=tid,
        description=description,
        diff_context_lines=_check_int(ctx["diff_context_lines"], 0, 1000, f"{where}.context.diff_context_lines"),
        include_full_files=ctx["include_full_files"],
        context_globs=check_globs(ctx.get("files", []), f"{where}.context.files"),
        max_file_bytes=_check_int(ctx["max_file_bytes"], 1, 10_000_000, f"{where}.context.max_file_bytes"),
        instructions=_check_text(rv["instructions"], f"{where}.review.instructions"),
        checklist=tuple(checklist),
        sections=parsed_sections,
        definition=definition,
        builtin=builtin,
    )


def parse_template_text(text: str, name: str = "template") -> dict:
    stripped = text.lstrip()
    try:
        if stripped.startswith("{"):
            return json.loads(text)
        return _toml.loads(text)
    except (ValueError, _toml.TOMLDecodeError) as e:
        raise TemplateError(f"{name}: cannot parse ({e})")


def load_template_file(path) -> PRTemplate:
    path = Path(path)
    try:
        text = path.read_text("utf-8")
    except OSError as e:
        raise TemplateError(f"cannot read template {path}: {e}")
    return parse_template(parse_template_text(text, path.name), where=path.name)


def _load_builtins() -> dict[str, PRTemplate]:
    out = {}
    for entry in sorted(resources.files("dosr_protocol").joinpath("templates").iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".toml"):
            t = parse_template(parse_template_text(entry.read_text("utf-8"), entry.name),
                               where=entry.name, builtin=True)
            if t.id in out:
                raise TemplateError(f"duplicate built-in template id {t.id!r}")
            out[t.id] = t
    return out


BUILTIN_TEMPLATES: dict[str, PRTemplate] = _load_builtins()


def find_templates(*dirs) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for d in dirs:
        if not d or not Path(d).is_dir():
            continue
        for path in sorted(Path(d).iterdir()):
            if path.suffix not in (".toml", ".json") or not path.is_file():
                continue
            try:
                t = load_template_file(path)
                if t.id in BUILTIN_TEMPLATES:
                    raise TemplateError(f"{path.name}: id {t.id!r} is a built-in template; pick another id")
                found.setdefault(t.id, {"template": t, "path": str(path)})
            except TemplateError as e:
                found[f"!{path.name}"] = {"error": str(e), "path": str(path)}
    return found


def to_toml(t: PRTemplate) -> str:
    d = t.definition

    def s(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, str):
            if "\n" in v and "'''" not in v and "\r" not in v:
                return f"'''\n{v}'''"
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, list):
            if not v:
                return "[]"
            return "[\n" + "".join(f"    {s(x)},\n" for x in v) + "]"
        raise TypeError(type(v))

    lines = [f"format = {s(d['format'])}", f"id = {s(d['id'])}", f"description = {s(d['description'])}", "",
             "[context]"]
    lines += [f"{k} = {s(v)}" for k, v in d["context"].items()]
    lines += ["", "[review]"] + [f"{k} = {s(v)}" for k, v in d["review"].items()]
    for sec in d.get("sections", []):
        lines += ["", "[[sections]]"] + [f"{k} = {s(v)}" for k, v in sec.items()]
    return "\n".join(lines) + "\n"
