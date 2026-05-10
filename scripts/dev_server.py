#!/usr/bin/env python3
"""Run the project locally without AWS.

Usage:  python scripts/dev_server.py

What it does:
1. Copies frontend/config.local.js → frontend/config.js  (machine-specific local config)
2. Starts the backend via uvicorn on port 8000
3. Starts a static file server for the frontend on port 3000
4. Opens http://localhost:3000 in the default browser
5. On Ctrl+C, terminates both processes cleanly

Secrets are NOT injected — local dev runs without AWS. Handle local secret
values in your own app code (e.g. a gitignored .env file or STAGE=local guard).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from infra.config_loader import load_config  # noqa: E402
from infra.config_schema import Ec2Backend, LambdaBackend, Stage  # noqa: E402

_YAML = Path("deploy.yaml")
_STAGE: Stage = "dev"


def copy_local_config(frontend_dir: Path) -> None:
    """Copy config.local.js → config.js so the frontend uses localhost URLs."""
    src = frontend_dir / "config.local.js"
    dst = frontend_dir / "config.js"
    if not src.exists():
        raise SystemExit(
            f"{src} not found. Create it with your local API URL, e.g.:\n"
            "  window.APP_CONFIG = { apiUrl: 'http://localhost:8000', stage: 'local' };"
        )
    shutil.copy(src, dst)


def _backend_dir(yaml_path: Path) -> Path:
    """Resolve the backend source directory from deploy.yaml."""
    cfg = load_config(yaml_path, _STAGE, validate_paths=False)
    if isinstance(cfg.backend, LambdaBackend):
        return Path(cfg.backend.lambdas[0].source_path)
    if isinstance(cfg.backend, Ec2Backend):
        return Path(cfg.backend.ec2.source_path)
    raise SystemExit("Unknown backend type in deploy.yaml.")


def main() -> None:
    yaml_path = _YAML

    cfg_for_frontend = load_config(yaml_path, _STAGE, validate_paths=False)
    frontend_dir = Path(cfg_for_frontend.frontend.source_path)
    copy_local_config(frontend_dir)

    backend_dir = _backend_dir(yaml_path)

    uvicorn_exe = shutil.which("uvicorn")
    if not uvicorn_exe:
        raise SystemExit("uvicorn not found in PATH. Install it: pip install 'uvicorn[standard]'")

    python_exe = shutil.which("python") or shutil.which("python3") or sys.executable

    print(f"Starting backend (uvicorn) in {backend_dir} on port 8000...")
    backend_proc = subprocess.Popen(
        [uvicorn_exe, "handler:app", "--reload", "--port", "8000"],
        cwd=backend_dir,
        shell=False,
    )

    print(f"Starting frontend (http.server) in {frontend_dir} on port 3000...")
    frontend_proc = subprocess.Popen(
        [python_exe, "-m", "http.server", "3000"],
        cwd=frontend_dir,
        shell=False,
    )

    print("Opening http://localhost:3000 ...")
    webbrowser.open("http://localhost:3000")
    print("Press Ctrl+C to stop.")

    try:
        backend_proc.wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        backend_proc.terminate()
        frontend_proc.terminate()
        backend_proc.wait()
        frontend_proc.wait()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
