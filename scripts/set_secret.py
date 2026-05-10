#!/usr/bin/env python3
"""Rotate or delete a secret in AWS Secrets Manager.

Usage:
    python scripts/set_secret.py <stage> <name>             # rotate (prompts for new value)
    python scripts/set_secret.py <stage> <name> --delete    # schedule deletion (y/N)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from infra.config_loader import load_config  # noqa: E402
from infra.config_schema import Stage  # noqa: E402
from infra.secrets_bootstrap import delete_secret, rotate_secret  # noqa: E402

_YAML = Path("deploy.yaml")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rotate or delete a declared secret in AWS Secrets Manager."
    )
    parser.add_argument("stage", choices=["prod", "dev"], help="Deployment stage")
    parser.add_argument("name", help="Secret name (env-var identifier, e.g. DB_PASSWORD)")
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Schedule the secret for deletion (7-day recovery window)",
    )
    args = parser.parse_args()

    stage = cast(Stage, args.stage)
    cfg = load_config(_YAML, stage, validate_paths=False)

    if args.delete:
        answer = input(
            f"Schedule secret {args.name!r} for deletion (stage={stage!r})? [y/N] "
        ).strip()
        if answer.lower() != "y":
            print("Cancelled.")
            return
        delete_secret(cfg, stage, args.name)
    else:
        rotate_secret(cfg, stage, args.name)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
