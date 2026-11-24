from dataclasses import dataclass
from pathlib import Path

from dosr_git import pr_inputs
from dosr_protocol.canonical import sha256_0x
from dosr_protocol.decision import DECISION_V1, build_prompt
from dosr_protocol.templates import TemplateError, load_template_file
from dosr_protocol.pr_format import (
    TEMPLATES,
    PRFormatError,
    PRTemplate,
    build_pr,
    check_limits,
    pr_metrics,
    render_markdown,
    text_metrics,
)

from .cases import Materialized
from .context import CONTEXTS, context_section

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"

EVAL_REPO_ID = "0x" + "e7" * 32

DEFAULT_LIMITS = {"review": {"max_changed_files": 20, "max_patch_bytes": 100_000}}


@dataclass
class ReviewInput:
    template: str
    prompt: str
    review_input_hash: str
    pr_hash: str
    metrics: dict
    limit_problems: list[str]
    context: str = "none"


def load_template(template_id: str, templates_dir: Path = TEMPLATES_DIR) -> PRTemplate:
    if template_id in TEMPLATES:
        return TEMPLATES[template_id]
    path = Path(templates_dir) / f"{template_id}.toml"
    if not path.is_file():
        raise PRFormatError(f"unknown template {template_id!r}: not built in and no {path}")
    try:
        template = load_template_file(path)
    except TemplateError as e:
        raise PRFormatError(str(e))
    if template.id != template_id:
        raise PRFormatError(f"{path}: id must be {template_id!r}")
    return template


def build_review_input(m: Materialized, template: PRTemplate, decision_schema: str = DECISION_V1,
                       limits: dict = DEFAULT_LIMITS, context: str = "none") -> ReviewInput:
    if context not in CONTEXTS:
        raise ValueError(f"unknown context {context!r}: expected one of {', '.join(CONTEXTS)}")
    inputs = pr_inputs(m.git, template, m.parent, m.candidate)
    pr = build_pr(
        template=template,
        repo={"name": m.case.repo_name, "repo_id": EVAL_REPO_ID},
        parent_git_oid=f"{m.object_format}:{m.parent}",
        candidate_git_oid=f"{m.object_format}:{m.candidate}",
        **inputs,
    )
    markdown = render_markdown(pr, template)
    extra = context_section(m, context, skip_paths=set(inputs["full_files"]))
    if extra:
        markdown = f"{markdown.rstrip()}\n\n{extra.rstrip()}\n"
    prompt = build_prompt(template, markdown, decision_schema)
    metrics = pr_metrics(pr, markdown)
    if extra:
        metrics["sections"]["related_code"] = text_metrics(extra)
    return ReviewInput(
        template=template.id,
        prompt=prompt,
        review_input_hash=sha256_0x(prompt.encode("utf-8")),
        pr_hash=pr["pr_hash"],
        metrics=metrics,
        limit_problems=check_limits(limits, inputs["files"], inputs["diff_text"]),
        context=context,
    )
