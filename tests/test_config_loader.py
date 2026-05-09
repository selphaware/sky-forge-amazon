from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from infra.config_loader import apply_stage_merge, deep_merge, load_config
from infra.config_schema import DeployConfig, Ec2Backend, LambdaBackend, SecretBackend

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _minimal_lambda_yaml() -> str:
    return dedent(
        """
        project: newapp
        base_domain: selpha.com
        aws_account: "123456789012"
        aws_region: us-east-1
        frontend:
          source_path: ./frontend
        backend:
          type: lambda
          lambdas:
            - name: api
              source_path: ./backend/api
              handler: handler.handler
              route_prefix: /
        security:
          cors:
            allowed_origins:
              - https://newapp.selpha.com
        stages:
          prod: {}
          dev:
            security:
              cors:
                allowed_origins: ["*"]
        """
    ).strip()


def _minimal_ec2_yaml() -> str:
    return dedent(
        """
        project: newapp
        base_domain: selpha.com
        aws_account: "123456789012"
        aws_region: us-east-1
        frontend:
          source_path: ./frontend
        backend:
          type: ec2
          ec2:
            instance_type: t3.small
            source_path: ./backend/app
            app_entrypoint: "uvicorn handler:app --host 0.0.0.0 --port 8000"
            app_port: 8000
        security:
          cors:
            allowed_origins:
              - https://newapp.selpha.com
        stages:
          prod: {}
          dev: {}
        """
    ).strip()


def _validate(raw: dict[str, Any], stage: str = "prod") -> DeployConfig:
    """Emulate the loader's stage-merge + inject + validate, no filesystem."""
    merged = apply_stage_merge(raw, stage)  # type: ignore[arg-type]
    merged["stage"] = stage
    return DeployConfig.model_validate(merged)


@pytest.fixture
def lambda_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "deploy.yaml"
    p.write_text(_minimal_lambda_yaml())
    return p


@pytest.fixture
def ec2_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "deploy.yaml"
    p.write_text(_minimal_ec2_yaml())
    return p


# --------------------------------------------------------------------------- #
# Happy paths                                                                 #
# --------------------------------------------------------------------------- #


def test_valid_lambda_loads(lambda_yaml: Path) -> None:
    cfg = load_config(lambda_yaml, "prod", validate_paths=False)
    assert isinstance(cfg.backend, LambdaBackend)
    assert cfg.backend.lambdas[0].name == "api"
    assert cfg.backend.lambdas[0].route_prefix == "/"
    assert cfg.backend.lambdas[0].memory == 512
    assert cfg.backend.lambdas[0].timeout == 30
    assert cfg.stage == "prod"
    assert cfg.secret_backend is SecretBackend.SECRETS_MANAGER


def test_valid_ec2_loads(ec2_yaml: Path) -> None:
    cfg = load_config(ec2_yaml, "prod", validate_paths=False)
    assert isinstance(cfg.backend, Ec2Backend)
    assert cfg.backend.ec2.instance_type == "t3.small"
    assert cfg.backend.ec2.health_check_path == "/health"
    assert cfg.stage == "prod"


# --------------------------------------------------------------------------- #
# Invalid backend / region / required fields                                  #
# --------------------------------------------------------------------------- #


def test_invalid_backend_type_rejected() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    raw["backend"] = {"type": "wsgi", "lambdas": []}
    with pytest.raises(ValidationError) as exc_info:
        _validate(raw)
    msg = str(exc_info.value)
    assert "backend" in msg or "discriminator" in msg.lower() or "type" in msg


def test_aws_region_must_be_us_east_1() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    raw["aws_region"] = "eu-west-1"
    with pytest.raises(ValidationError) as exc_info:
        _validate(raw)
    assert "aws_region" in str(exc_info.value) or "us-east-1" in str(exc_info.value)


def test_missing_required_field_names_field() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    del raw["project"]
    with pytest.raises(ValidationError) as exc_info:
        _validate(raw)
    assert "project" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Stage merge                                                                 #
# --------------------------------------------------------------------------- #


def test_stage_override_deep_merges(lambda_yaml: Path) -> None:
    cfg_dev = load_config(lambda_yaml, "dev", validate_paths=False)
    assert cfg_dev.security.cors.allowed_origins == ["*"]

    cfg_prod = load_config(lambda_yaml, "prod", validate_paths=False)
    assert cfg_prod.security.cors.allowed_origins == ["https://newapp.selpha.com"]


def test_deep_merge_lists_replaced() -> None:
    base = {"a": [1, 2, 3], "b": {"x": 1, "y": 2}}
    override = {"a": [9], "b": {"y": 20, "z": 30}}
    result = deep_merge(base, override)
    assert result == {"a": [9], "b": {"x": 1, "y": 20, "z": 30}}


def test_deep_merge_does_not_mutate_inputs() -> None:
    base = {"a": [1, 2], "nested": {"x": 1}}
    override = {"nested": {"y": 2}}
    deep_merge(base, override)
    assert base == {"a": [1, 2], "nested": {"x": 1}}
    assert override == {"nested": {"y": 2}}


# --------------------------------------------------------------------------- #
# Route-prefix overlap                                                        #
# --------------------------------------------------------------------------- #


def _two_lambdas(prefixes: tuple[str, str]) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(_minimal_lambda_yaml())
    raw["backend"]["lambdas"] = [
        {
            "name": "lam-a",
            "source_path": "./a",
            "handler": "h.h",
            "route_prefix": prefixes[0],
        },
        {
            "name": "lam-b",
            "source_path": "./b",
            "handler": "h.h",
            "route_prefix": prefixes[1],
        },
    ]
    return raw


def test_overlapping_route_prefix_segment_wise_rejected() -> None:
    raw = _two_lambdas(("/users", "/users/admin"))
    with pytest.raises(ValidationError) as exc_info:
        _validate(raw)
    assert "overlap" in str(exc_info.value).lower()


def test_non_overlapping_similar_prefixes_accepted() -> None:
    # /users and /users-admin must NOT be flagged — they're sibling resources at the
    # root, not parent/child. Naive startswith() would falsely flag this.
    raw = _two_lambdas(("/users", "/users-admin"))
    cfg = _validate(raw)
    assert isinstance(cfg.backend, LambdaBackend)


def test_catchall_route_prefix_with_others_rejected() -> None:
    raw = _two_lambdas(("/", "/users"))
    with pytest.raises(ValidationError) as exc_info:
        _validate(raw)
    msg = str(exc_info.value).lower()
    assert "catch-all" in msg or "/" in msg


def test_route_prefix_must_start_with_slash() -> None:
    raw = _two_lambdas(("users", "/orders"))
    with pytest.raises(ValidationError) as exc_info:
        _validate(raw)
    assert "route_prefix" in str(exc_info.value) or "/" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Secrets: defaults, collisions, name validation                              #
# --------------------------------------------------------------------------- #


def test_aws_secret_name_default_resolution() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    raw["backend"]["lambdas"][0]["secrets"] = [{"name": "DB_PASSWORD"}]
    cfg = _validate(raw, "prod")
    assert isinstance(cfg.backend, LambdaBackend)
    sec = cfg.backend.lambdas[0].secrets[0]
    assert sec.aws_secret_name == "newapp/prod/db_password"


def test_secret_prompt_and_description_defaulted() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    raw["backend"]["lambdas"][0]["secrets"] = [{"name": "DB_PASSWORD"}]
    cfg = _validate(raw, "prod")
    assert isinstance(cfg.backend, LambdaBackend)
    sec = cfg.backend.lambdas[0].secrets[0]
    assert sec.prompt == "DB_PASSWORD"
    assert sec.description == "DB_PASSWORD for newapp (prod)"


def test_secret_explicit_overrides_kept() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    raw["backend"]["lambdas"][0]["secrets"] = [
        {
            "name": "DB_PASSWORD",
            "prompt": "Database password",
            "description": "RDS master pw",
            "aws_secret_name": "newapp/shared/db_password",
        }
    ]
    cfg = _validate(raw, "prod")
    assert isinstance(cfg.backend, LambdaBackend)
    sec = cfg.backend.lambdas[0].secrets[0]
    assert sec.prompt == "Database password"
    assert sec.description == "RDS master pw"
    assert sec.aws_secret_name == "newapp/shared/db_password"


def test_secret_name_collision_rejected_lambda() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    raw["backend"]["lambdas"][0]["secrets"] = [
        {"name": "DB_PASSWORD"},
        {"name": "OTHER", "aws_secret_name": "newapp/prod/db_password"},
    ]
    with pytest.raises(ValidationError) as exc_info:
        _validate(raw, "prod")
    assert "newapp/prod/db_password" in str(exc_info.value)


def test_secret_name_collision_rejected_ec2() -> None:
    raw = yaml.safe_load(_minimal_ec2_yaml())
    raw["backend"]["ec2"]["secrets"] = [
        {"name": "DB_PASSWORD"},
        {"name": "OTHER", "aws_secret_name": "newapp/prod/db_password"},
    ]
    with pytest.raises(ValidationError) as exc_info:
        _validate(raw, "prod")
    assert "newapp/prod/db_password" in str(exc_info.value)


def test_stage_segregation_no_collision() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    raw["backend"]["lambdas"][0]["secrets"] = [{"name": "DB_PASSWORD"}]
    cfg_prod = _validate(raw, "prod")
    cfg_dev = _validate(raw, "dev")
    assert isinstance(cfg_prod.backend, LambdaBackend)
    assert isinstance(cfg_dev.backend, LambdaBackend)
    prod_sec = cfg_prod.backend.lambdas[0].secrets[0]
    dev_sec = cfg_dev.backend.lambdas[0].secrets[0]
    assert prod_sec.aws_secret_name == "newapp/prod/db_password"
    assert dev_sec.aws_secret_name == "newapp/dev/db_password"


@pytest.mark.parametrize("bad_name", ["db_password", "db-password", "1ABC", "abc", ""])
def test_invalid_secret_name_rejected(bad_name: str) -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    raw["backend"]["lambdas"][0]["secrets"] = [{"name": bad_name}]
    with pytest.raises(ValidationError):
        _validate(raw)


def test_aws_secret_name_leading_slash_rejected() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    raw["backend"]["lambdas"][0]["secrets"] = [
        {"name": "DB_PASSWORD", "aws_secret_name": "/newapp/prod/db_password"}
    ]
    with pytest.raises(ValidationError):
        _validate(raw)


def test_aws_secret_name_max_length_enforced() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    raw["backend"]["lambdas"][0]["secrets"] = [
        {"name": "DB_PASSWORD", "aws_secret_name": "a" * 513}
    ]
    with pytest.raises(ValidationError):
        _validate(raw)


# --------------------------------------------------------------------------- #
# extra="forbid" / loader strips stages                                       #
# --------------------------------------------------------------------------- #


def test_unknown_top_level_key_rejected() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    raw["unknown_field"] = "anything"
    with pytest.raises(ValidationError) as exc_info:
        _validate(raw)
    assert "unknown_field" in str(exc_info.value) or "extra" in str(exc_info.value).lower()


def test_loader_strips_stages_key() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    cfg = _validate(raw, "prod")
    # `stages` is not a DeployConfig field; the loader removes it before validation.
    assert "stages" not in DeployConfig.model_fields
    assert not hasattr(cfg, "stages")
    # And a YAML containing `stages:` doesn't trip extra="forbid" — proven by the
    # fact that this validation succeeded.


def test_unknown_stage_rejected() -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    with pytest.raises(ValueError) as exc_info:
        _validate(raw, "staging")
    assert "staging" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# base_domain validator                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    ["nodot", ".leading", "trailing.", "with space.com", ""],
)
def test_invalid_base_domain_rejected(bad: str) -> None:
    raw = yaml.safe_load(_minimal_lambda_yaml())
    raw["base_domain"] = bad
    with pytest.raises(ValidationError):
        _validate(raw)


# --------------------------------------------------------------------------- #
# Path-existence check (loader integration)                                   #
# --------------------------------------------------------------------------- #


def test_validate_paths_failure(tmp_path: Path) -> None:
    yaml_path = tmp_path / "deploy.yaml"
    yaml_path.write_text(_minimal_lambda_yaml())
    # ./frontend and ./backend/api don't exist under tmp_path.
    with pytest.raises(FileNotFoundError) as exc_info:
        load_config(yaml_path, "prod", validate_paths=True)
    assert "source_path" in str(exc_info.value)


def test_validate_paths_success(tmp_path: Path) -> None:
    yaml_path = tmp_path / "deploy.yaml"
    yaml_path.write_text(_minimal_lambda_yaml())
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend" / "api").mkdir(parents=True)
    cfg = load_config(yaml_path, "prod", validate_paths=True)
    assert isinstance(cfg.backend, LambdaBackend)
