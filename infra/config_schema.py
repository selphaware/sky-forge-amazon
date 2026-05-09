"""Pydantic schema for the resolved (single-stage) deploy.yaml.

The loader strips the ``stages`` block and deep-merges the active stage onto the
top-level dict before validation, then injects ``stage`` so the post-validator
can resolve secret defaults.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Stage = Literal["prod", "dev"]


class SecretBackend(StrEnum):
    # v2 will add PARAMETER_STORE; the loader-resolved config carries this enum
    # so stack code never sees the literal "secretsmanager" string.
    SECRETS_MANAGER = "secrets_manager"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Leaf models                                                                 #
# --------------------------------------------------------------------------- #


class FrontendConfig(_Base):
    source_path: str
    index_document: str = "index.html"
    error_document: str | None = None


class CorsConfig(_Base):
    allowed_origins: list[str] = Field(default_factory=list)


class SecurityConfig(_Base):
    cors: CorsConfig = Field(default_factory=CorsConfig)
    api_key_required: bool = False
    ip_allowlist: list[str] = Field(default_factory=list)


class IamGrant(_Base):
    actions: list[str] = Field(min_length=1)
    resources: list[str] = Field(min_length=1)


class SecretConfig(_Base):
    name: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")
    prompt: str | None = None
    description: str | None = None
    aws_secret_name: str | None = Field(
        default=None,
        max_length=512,
        # First char must not be '/'; remaining chars allow '/' for path-style names.
        pattern=r"^[A-Za-z0-9_+=.@-][A-Za-z0-9/_+=.@-]*$",
    )


class LambdaVpcConfig(_Base):
    vpc_id: str
    subnet_ids: list[str] = Field(min_length=1)
    security_group_ids: list[str] = Field(min_length=1)


class Ec2VpcConfig(_Base):
    vpc_id: str
    public_subnet_ids: list[str] = Field(min_length=1)
    private_subnet_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Backend variants (discriminated union)                                      #
# --------------------------------------------------------------------------- #


class LambdaConfig(_Base):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,30}$")
    source_path: str
    handler: str
    runtime: Literal["python3.12"] = "python3.12"
    memory: int = Field(default=512, ge=128, le=10240)
    timeout: int = Field(default=30, ge=1, le=900)
    route_prefix: str
    env_vars: dict[str, str] = Field(default_factory=dict)
    secrets: list[SecretConfig] = Field(default_factory=list)
    iam_grants: list[IamGrant] = Field(default_factory=list)
    vpc: LambdaVpcConfig | None = None

    @field_validator("route_prefix")
    @classmethod
    def _route_prefix_starts_with_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("route_prefix must start with '/'")
        return v


class Ec2Config(_Base):
    instance_type: str
    ami_id: str | None = None
    key_name: str | None = None
    source_path: str
    app_entrypoint: str
    app_port: int = Field(default=8000, ge=1, le=65535)
    health_check_path: str = "/health"
    env_vars: dict[str, str] = Field(default_factory=dict)
    secrets: list[SecretConfig] = Field(default_factory=list)
    iam_grants: list[IamGrant] = Field(default_factory=list)
    vpc: Ec2VpcConfig | None = None
    private_subnet: bool = False

    @field_validator("health_check_path")
    @classmethod
    def _health_check_path_starts_with_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("health_check_path must start with '/'")
        return v

    @model_validator(mode="after")
    def _private_subnet_requires_subnet_ids(self) -> Ec2Config:
        if self.private_subnet and (self.vpc is None or not self.vpc.private_subnet_ids):
            raise ValueError("private_subnet=true requires vpc.private_subnet_ids to be non-empty")
        return self


class LambdaBackend(_Base):
    type: Literal["lambda"]
    lambdas: list[LambdaConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_route_prefix_overlaps(self) -> LambdaBackend:
        prefixes = [lam.route_prefix for lam in self.lambdas]
        if len(prefixes) <= 1:
            return self
        # Catch-all "/" must be alone.
        if "/" in prefixes:
            others = sorted({p for p in prefixes if p != "/"})
            if others:
                raise ValueError(
                    f"route_prefix '/' is a catch-all and cannot coexist with other "
                    f"prefixes: {others}"
                )
            # Multiple "/" prefixes — also overlapping.
            if prefixes.count("/") > 1:
                raise ValueError("route_prefix '/' is declared on more than one lambda")
            return self
        # Segment-aware overlap: a is a path-prefix of b iff a's segments are a leading
        # slice of b's segments. This avoids false positives like /users vs /users-admin.
        seg_lists = [_segments(p) for p in prefixes]
        for i in range(len(seg_lists)):
            for j in range(i + 1, len(seg_lists)):
                if _segments_overlap(seg_lists[i], seg_lists[j]):
                    raise ValueError(f"route_prefixes overlap: {prefixes[i]!r} and {prefixes[j]!r}")
        return self


class Ec2Backend(_Base):
    type: Literal["ec2"]
    ec2: Ec2Config


BackendConfig = Annotated[LambdaBackend | Ec2Backend, Field(discriminator="type")]


# --------------------------------------------------------------------------- #
# Top-level config                                                            #
# --------------------------------------------------------------------------- #


class DeployConfig(_Base):
    project: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    base_domain: str
    aws_account: str = Field(pattern=r"^\d{12}$")
    aws_region: Literal["us-east-1"]
    frontend: FrontendConfig
    backend: BackendConfig
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    secret_backend: SecretBackend = SecretBackend.SECRETS_MANAGER
    # Loader-managed: the loader injects this after stage merge. Do not set in YAML.
    stage: Stage

    @field_validator("base_domain")
    @classmethod
    def _check_base_domain(cls, v: str) -> str:
        if not v or any(c.isspace() for c in v):
            raise ValueError("base_domain must be non-empty and contain no whitespace")
        if v.startswith(".") or v.endswith("."):
            raise ValueError("base_domain must not start or end with '.'")
        if "." not in v:
            raise ValueError("base_domain must contain at least one '.'")
        return v

    @model_validator(mode="after")
    def _resolve_and_check_secrets(self) -> DeployConfig:
        all_secrets = list(_iter_secrets(self))
        for path, sec in all_secrets:
            _ = path  # path used only in collision messages
            if sec.aws_secret_name is None:
                sec.aws_secret_name = f"{self.project}/{self.stage}/{sec.name.lower()}"
            if sec.prompt is None:
                sec.prompt = sec.name
            if sec.description is None:
                sec.description = f"{sec.name} for {self.project} ({self.stage})"

        by_aws_name: dict[str, list[str]] = {}
        for path, sec in all_secrets:
            assert sec.aws_secret_name is not None  # filled in above
            by_aws_name.setdefault(sec.aws_secret_name, []).append(path)

        collisions = {n: paths for n, paths in by_aws_name.items() if len(paths) > 1}
        if collisions:
            lines = [
                f"  - aws_secret_name {name!r} declared at: {', '.join(paths)}"
                for name, paths in collisions.items()
            ]
            raise ValueError("Secret name collisions within stage:\n" + "\n".join(lines))

        return self


# --------------------------------------------------------------------------- #
# Internal helpers                                                            #
# --------------------------------------------------------------------------- #


def _segments(path: str) -> tuple[str, ...]:
    return tuple(s for s in path.split("/") if s)


def _segments_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return long[: len(short)] == short


def _iter_secrets(cfg: DeployConfig) -> list[tuple[str, SecretConfig]]:
    out: list[tuple[str, SecretConfig]] = []
    if isinstance(cfg.backend, LambdaBackend):
        for i, lam in enumerate(cfg.backend.lambdas):
            for j, sec in enumerate(lam.secrets):
                out.append((f"backend.lambdas[{i}].secrets[{j}]", sec))
    elif isinstance(cfg.backend, Ec2Backend):
        for j, sec in enumerate(cfg.backend.ec2.secrets):
            out.append((f"backend.ec2.secrets[{j}]", sec))
    return out
