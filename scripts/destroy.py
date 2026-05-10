#!/usr/bin/env python3
"""Destroy a sky-forge-amazon stack after y/N confirmation.

Usage:  python scripts/destroy.py <stage>

Secrets in Secrets Manager are NOT touched — they are decoupled from the stack
by design and must be cleaned up explicitly via set_secret.py --delete.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from infra.config_loader import load_config  # noqa: E402
from infra.config_schema import Stage  # noqa: E402

_YAML = Path("deploy.yaml")


def confirm_destroy(stack_name: str) -> bool:
    """Return True only if the user explicitly types 'y'."""
    try:
        answer = input(f"Destroy stack {stack_name!r}? This cannot be undone. [y/N] ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return False
    return answer.lower() == "y"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/destroy.py <stage>")

    stage_raw = sys.argv[1]
    if stage_raw not in ("prod", "dev"):
        raise SystemExit(f"Invalid stage {stage_raw!r}. Must be 'prod' or 'dev'.")
    stage = cast(Stage, stage_raw)

    cfg = load_config(_YAML, stage, validate_paths=False)
    stack_name = f"{cfg.project}-{cfg.stage}"

    if not confirm_destroy(stack_name):
        print("Cancelled.")
        return

    exe = shutil.which("cdk") or shutil.which("cdk.cmd")
    if not exe:
        raise SystemExit("cdk CLI not found in PATH.")

    # --force skips CDK's own interactive confirmation (we already asked above).
    result = subprocess.run([exe, "destroy", stack_name, "--force"], shell=False)
    if result.returncode != 0:
        raise SystemExit(f"cdk destroy exited with code {result.returncode}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
