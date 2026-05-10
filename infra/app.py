"""CDK app entry point.

Reads `stage` and `config` from CDK context (`-c stage=prod -c config=path`),
loads the YAML, and instantiates the appropriate stack with an explicit
``env=`` so HostedZone.from_lookup has account+region available.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import aws_cdk as cdk

from infra.config_loader import load_config
from infra.config_schema import Stage
from infra.stacks.ec2_stack import Ec2Stack
from infra.stacks.lambda_stack import LambdaStack


def main() -> None:
    app = cdk.App()

    stage_raw = app.node.try_get_context("stage")
    if stage_raw not in ("prod", "dev"):
        raise SystemExit(f"-c stage=prod|dev is required (got {stage_raw!r})")
    stage = cast(Stage, stage_raw)

    yaml_path = Path(app.node.try_get_context("config") or "deploy.yaml")
    cfg = load_config(yaml_path, stage, validate_paths=True)

    # `-c bundle=false` disables Docker bundling. Useful for synth-only smoke
    # tests and CI environments that don't have Docker.
    bundle_ctx = app.node.try_get_context("bundle")
    bundle_lambdas = str(bundle_ctx).lower() != "false"

    env = cdk.Environment(account=cfg.aws_account, region=cfg.aws_region)
    stack_name = f"{cfg.project}-{cfg.stage}"

    if cfg.backend.type == "lambda":
        LambdaStack(app, stack_name, cfg=cfg, env=env, bundle_lambdas=bundle_lambdas)
    elif cfg.backend.type == "ec2":
        Ec2Stack(app, stack_name, cfg=cfg, env=env)
    else:
        raise SystemExit(f"backend.type='{cfg.backend.type}' is not supported")

    app.synth()


if __name__ == "__main__":
    main()
