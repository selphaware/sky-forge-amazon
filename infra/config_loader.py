"""YAML loading + stage-merge for the deploy config.

The loader is the only thing that knows about the ``stages:`` block, deep-merge
semantics, and on-disk file paths. Anything downstream consumes a fully-resolved
single-stage :class:`DeployConfig`.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from infra.config_schema import DeployConfig, Ec2Backend, LambdaBackend, Stage


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``override`` into ``base``. Lists are replaced wholesale."""
    result = deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = deep_merge(result[key], val)
        else:
            result[key] = deepcopy(val)
    return result


def apply_stage_merge(raw: dict[str, Any], stage: Stage) -> dict[str, Any]:
    """Strip ``stages:`` and deep-merge ``stages.<stage>`` onto the top-level dict."""
    if "stages" not in raw:
        raise ValueError("deploy.yaml must contain a 'stages' block")
    stages = raw["stages"]
    if not isinstance(stages, dict):
        raise ValueError("'stages' must be a mapping")
    if stage not in stages:
        available = sorted(str(k) for k in stages)
        raise ValueError(f"stage {stage!r} not found in stages: {available}")

    base = {k: v for k, v in raw.items() if k != "stages"}
    override = stages[stage] or {}
    if not isinstance(override, dict):
        raise ValueError(f"stages.{stage} must be a mapping (got {type(override).__name__})")
    return deep_merge(base, override)


def load_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping (got {type(data).__name__} from {path})")
    return data


def load_config(
    yaml_path: Path,
    stage: Stage,
    *,
    validate_paths: bool = True,
) -> DeployConfig:
    """Load deploy.yaml, apply stage merge, validate, optionally check source paths exist."""
    raw = load_yaml(yaml_path)
    merged = apply_stage_merge(raw, stage)
    merged["stage"] = stage  # loader-injected; the schema's post-validator reads it
    cfg = DeployConfig.model_validate(merged)

    if validate_paths:
        _validate_paths_exist(cfg, yaml_path.parent)
    return cfg


def _validate_paths_exist(cfg: DeployConfig, base_dir: Path) -> None:
    paths_to_check: list[tuple[str, str]] = [
        ("frontend.source_path", cfg.frontend.source_path),
    ]
    if isinstance(cfg.backend, LambdaBackend):
        for i, lam in enumerate(cfg.backend.lambdas):
            paths_to_check.append((f"backend.lambdas[{i}].source_path", lam.source_path))
    elif isinstance(cfg.backend, Ec2Backend):
        paths_to_check.append(("backend.ec2.source_path", cfg.backend.ec2.source_path))

    for field, raw_path in paths_to_check:
        p = Path(raw_path)
        resolved = p if p.is_absolute() else (base_dir / p)
        if not resolved.exists():
            raise FileNotFoundError(
                f"{field}: '{raw_path}' does not exist (resolved to {resolved})"
            )
