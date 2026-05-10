"""Secret bootstrapping and management against AWS Secrets Manager.

Extracted from deploy.py so the secret lifecycle is independent of the deploy
orchestration. A v2 SSM Parameter Store toggle is a one-file change here
(swap the SecretBackend branch) rather than touching deploy.py.
"""

from __future__ import annotations

import getpass
import sys
from typing import Any

import boto3

from infra.config_schema import (
    DeployConfig,
    Ec2Backend,
    LambdaBackend,
    SecretConfig,
    Stage,
)


def _collect_secrets(cfg: DeployConfig) -> list[SecretConfig]:
    """Return every SecretConfig declared in the resolved (single-stage) config."""
    if isinstance(cfg.backend, LambdaBackend):
        return [sec for lam in cfg.backend.lambdas for sec in lam.secrets]
    if isinstance(cfg.backend, Ec2Backend):
        return list(cfg.backend.ec2.secrets)
    return []


def _find_secret(cfg: DeployConfig, name: str) -> SecretConfig:
    """Locate a SecretConfig by env-var name; raise SystemExit if not found."""
    for sec in _collect_secrets(cfg):
        if sec.name == name:
            return sec
    raise SystemExit(f"Secret {name!r} is not declared in deploy.yaml for stage {cfg.stage!r}.")


def bootstrap_secrets(cfg: DeployConfig) -> None:
    """Ensure every declared secret exists in Secrets Manager.

    - Existing secrets are left untouched (idempotent).
    - Missing secrets trigger a getpass prompt; empty input aborts cleanly.
    - Secrets scheduled for deletion cause a hard abort with a restore hint.
    - After ensuring all secrets, orphans (project-tagged secrets no longer in
      YAML) are listed with a warning but are never deleted automatically.
    """
    secrets = _collect_secrets(cfg)
    if not secrets:
        return

    client: Any = boto3.client("secretsmanager", region_name=cfg.aws_region)

    for sec in secrets:
        assert sec.aws_secret_name is not None
        try:
            desc = client.describe_secret(SecretId=sec.aws_secret_name)
            if "DeletedDate" in desc:
                raise SystemExit(
                    f"Secret {sec.aws_secret_name!r} is scheduled for deletion.\n"
                    "Restore it before deploying:\n"
                    f"  aws secretsmanager restore-secret "
                    f"--secret-id {sec.aws_secret_name}\n"
                    "Or wait for the 7-day recovery window to elapse."
                )
            print(f"  {sec.aws_secret_name}: exists, value unchanged")
        except client.exceptions.ResourceNotFoundException:
            prompt = sec.prompt or sec.name
            try:
                value = getpass.getpass(f"  Enter {prompt}: ")
            except KeyboardInterrupt:
                print("\nAborted.")
                sys.exit(1)
            if not value:
                raise SystemExit("Empty input — aborting. No secret was written.") from None
            client.create_secret(
                Name=sec.aws_secret_name,
                SecretString=value,
                Description=(sec.description or f"{sec.name} for {cfg.project} ({cfg.stage})"),
                Tags=[{"Key": "Project", "Value": cfg.project}],
            )
            print(f"  {sec.aws_secret_name}: created")

    _warn_orphans(cfg, client, secrets)


def _warn_orphans(cfg: DeployConfig, client: Any, active_secrets: list[SecretConfig]) -> None:
    """Print a warning for project-tagged secrets absent from the current YAML."""
    active_names = {sec.aws_secret_name for sec in active_secrets}
    paginator = client.get_paginator("list_secrets")
    for page in paginator.paginate(Filters=[{"Key": "tag-key", "Values": ["Project"]}]):
        for entry in page.get("SecretList", []):
            tags = {t["Key"]: t["Value"] for t in entry.get("Tags", [])}
            if tags.get("Project") == cfg.project and entry["Name"] not in active_names:
                print(
                    f"  WARNING: {entry['Name']!r} is tagged Project={cfg.project!r} "
                    "but is not declared in the current deploy.yaml. "
                    "To clean up: python scripts/set_secret.py <stage> <name> --delete"
                )


def rotate_secret(cfg: DeployConfig, stage: Stage, name: str) -> None:
    """Prompt for a new value and call put_secret_value. Requires redeploy."""
    sec = _find_secret(cfg, name)
    assert sec.aws_secret_name is not None

    client: Any = boto3.client("secretsmanager", region_name=cfg.aws_region)

    try:
        value = getpass.getpass(f"New value for {sec.prompt or sec.name}: ")
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
    if not value:
        raise SystemExit("Empty input — aborting. Secret was not updated.")

    client.put_secret_value(SecretId=sec.aws_secret_name, SecretString=value)
    print(f"  {sec.aws_secret_name}: updated.")
    print(
        f"  Redeploy to apply: python scripts/deploy.py {stage}\n"
        "  (v1 fetches secrets at deploy time, not at runtime)"
    )


def delete_secret(cfg: DeployConfig, stage: Stage, name: str) -> None:
    """Schedule the secret for deletion with a 7-day recovery window."""
    sec = _find_secret(cfg, name)
    assert sec.aws_secret_name is not None

    client: Any = boto3.client("secretsmanager", region_name=cfg.aws_region)
    client.delete_secret(SecretId=sec.aws_secret_name, RecoveryWindowInDays=7)
    print(
        f"  {sec.aws_secret_name!r} scheduled for deletion "
        "(recoverable for 7 days via "
        f"'aws secretsmanager restore-secret --secret-id {sec.aws_secret_name}')."
    )
