from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = "0.1"

GitOid = Field(pattern=r"^(sha1:[0-9a-f]{40}|sha256:[0-9a-f]{64})$")
Bytes32 = Field(pattern=r"^0x[0-9a-fA-F]{64}$")
Address = Field(pattern=r"^0x[0-9a-fA-F]{40}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Repository(Strict):
    repo_id: str = Bytes32
    chain_id: int
    contract: str = Address
    policy_hash: str = Bytes32


class Bundle(Strict):
    format: Literal["git-bundle-v1"]
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ipfs_cid: str | None = None


class Transition(Strict):
    parent_git_oid: str = GitOid
    candidate_git_oid: str = GitOid
    bundle: Bundle


class ReviewSpec(Strict):
    provider: str
    model: str
    model_version: str


class ClientInfo(Strict):
    client_version: str


class ReviewRequest(Strict):
    protocol_version: Literal["0.1"]
    request_id: str = Field(min_length=1)
    repository: Repository
    transition: Transition
    review: ReviewSpec
    client: ClientInfo


class PolicyGit(Strict):
    object_format: Literal["sha1", "sha256"]


class ApprovedModel(Strict):
    provider: str
    model: str
    version: str


class PolicyReview(Strict):
    approved_models: list[ApprovedModel] = Field(min_length=1)
    minimum_approvals: int = Field(ge=1)
    prompt_template: str
    decision_schema: str
    max_changed_files: int = Field(ge=1)
    max_patch_bytes: int = Field(ge=1)
    context_files: list[str] = Field(default_factory=list, max_length=50)
    template: dict[str, Any] | None = None


class PolicyAttestor(Strict):
    key_id: str
    scheme: Literal["eip712-secp256k1"]
    address: str = Address


class PolicyStorage(Strict):
    bundle_format: Literal["git-bundle-v1"]
    distribution: str


class Policy(Strict):
    protocol_version: Literal["0.1"]
    policy_id: str
    git: PolicyGit
    review: PolicyReview
    attestors: list[PolicyAttestor] = Field(min_length=1)
    storage: PolicyStorage
