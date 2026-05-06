# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo state

This repository currently contains only `PRD.md` — the implementation has not started. `PRD.md` is the single source of truth for what to build; read it before making structural decisions. The target structure (frontend/, backend/, infra/, scripts/, tests/, examples/) does not yet exist and should be created as work progresses.

## What this project is

A reusable AWS deployment template that takes a project (static HTML/JS frontend + Python backend) and deploys it to a custom domain on AWS. A single `deploy.yaml` selects between two backend architectures — **Lambda** (API Gateway + Lambda functions) or **EC2** (single instance behind ALB) — and drives the entire deploy. Frontend is always S3 + CloudFront + Route53 + ACM.

Two stages: `prod` and `dev`, with subdomain conventions `<project>.<base_domain>` / `<project>.dev.<base_domain>` and APIs at `api.<...>`.

## Model usage

Use **Opus** for design and hard work; use **Sonnet** for routine implementation. Concretely:

**Opus for:**
- Designing the Pydantic config schema and stage-merge semantics (`config_schema.py`, `config_loader.py`)
- Designing the CDK stack structure and resource wiring for both `LambdaStack` and `Ec2Stack`
- Anything involving CloudFront + ACM + Route53 + OAC interactions (subtle, easy to get wrong)
- The EC2 user data script and systemd unit (one-shot, hard to debug remotely)
- Debugging CDK synth or deploy failures
- Any decision that affects more than one file or trades off between approaches

**Sonnet for:**
- Writing the example apps in `examples/lambda-minimal/` and `examples/ec2-minimal/` (FastAPI handlers, simple HTML/JS)
- Boilerplate: `pyproject.toml`, `.gitignore`, `.pre-commit-config.yaml`, `requirements*.txt`
- Test scaffolding once the schema is designed (filling in `test_config_loader.py` cases)
- The `.github/workflows/ci.yml`
- README content
- Mechanical edits: renames, formatting fixes, adding type hints

If a Sonnet session hits a design question or a non-trivial CDK error, escalate to Opus rather than guessing.

## Implementation order

The order is intentional — it lets each step be verified before the next builds on it. Do not parallelise or one-shot the whole template; the synth/test feedback loop catches design mistakes much earlier than the deploy loop.

1. **Tooling foundation** — `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `.gitignore`, `.pre-commit-config.yaml`. Verify the hooks install and run without error (most are no-ops at this stage since there's no Python source yet).
2. **Config layer** — `infra/config_schema.py` + `infra/config_loader.py` + `tests/test_config_loader.py`. Pure Python, no AWS, no CDK. Verify `pytest` passes.
3. **Lambda path end-to-end** — `infra/stacks/lambda_stack.py` + `infra/app.py` + `examples/lambda-minimal/` + the Lambda half of `tests/test_stack_synth.py`. Verify synth via `cdk synth` and via the synth test. **Stop here for review before continuing.**
4. **EC2 path end-to-end** — `infra/stacks/ec2_stack.py` + `examples/ec2-minimal/` + EC2 half of synth tests. Verify synth.
5. **Scripts** — `scripts/deploy.py`, `scripts/destroy.py`, `scripts/dev_server.py`. Each tested manually with the example configs.
6. **CI + README** — `.github/workflows/ci.yml`, top-level `README.md`. Last because it documents what now exists.

After each step, the repo should be in a working, testable state. If a step can't be completed without making decisions outside the PRD, stop and ask rather than guessing.

## Platform constraints (load-bearing)

- **Windows-only host.** No WSL or Git Bash assumed. All scripts must run natively in cmd/PowerShell.
- **`PRD.md` shows `.sh` scripts in `scripts/` — implement them as Python scripts instead** (`deploy.py`, `destroy.py`, `dev_server.py`). The PRD explicitly authorizes and requires this conversion (§10).
- For all file paths: `pathlib.Path`, never string concatenation.
- For all subprocess calls: pass `args` as a list, never `shell=True`. The `cdk` CLI on Windows is `cdk.cmd` — `subprocess` resolves it via PATH; fall back to `shutil.which("cdk")` if needed.
- File operations (zip, copy, mkdir): `shutil` + `zipfile` stdlib, not shell commands.

## Python environment

- venv lives at `env_sky/` in the repo root (gitignored). Activate with `env_sky\Scripts\Activate.ps1` (PowerShell) or `env_sky\Scripts\activate` (cmd).
- All Python commands run with this venv activated.
- Target Python: **3.12+**.
- Tech stack: `aws-cdk-lib` v2, Pydantic v2, PyYAML.

## Common commands (once implemented)

```powershell
# Deploy
python scripts/deploy.py dev      # or prod

# Destroy (prompts y/N)
python scripts/destroy.py dev

# Local dev (no AWS calls; frontend at :3000, backend at :8000)
python scripts/dev_server.py

# Tests (synthesis-only, no AWS credentials needed, must run in <30s)
pytest

# Run a single test
pytest -k "stage_merge" -v

# Lint/format/type-check (via pre-commit)
pre-commit run --all-files
ruff format
ruff check --fix
mypy infra tests
```

## Architecture notes that span multiple files

### Config flow
`deploy.yaml` → `infra/config_loader.py` (PyYAML load + stage deep-merge) → `infra/config_schema.py` (Pydantic v2 validation, fail fast on misconfig, unknown keys are errors) → `scripts/deploy.py` (orchestrates) → `infra/app.py` (CDK entry, reads stage from `-c stage=...` context) → `infra/stacks/lambda_stack.py` or `ec2_stack.py`.

### Stage merge semantics
`stages.<stage>` blocks override top-level keys via deep merge. `prod: {}` means "use defaults"; `dev:` blocks typically loosen CORS and IP allowlist. Apply overrides in `config_loader.py` before passing to CDK — CDK never sees the raw multi-stage YAML.

### Stack selection
One CDK `Stack` subclass per architecture. `app.py` instantiates `LambdaStack` or `Ec2Stack` based on `backend.type`. Stack name is `f"{project}-{stage}"`. All resources tagged `Project=<project>`, `Stage=<stage>`.

### Frontend `config.js` is generated, not committed
`scripts/deploy.py` writes `frontend/config.js` (with `apiUrl`, `stage`, optional `apiKey`) before `cdk deploy`. `dev_server.py` copies `config.local.js` → `config.js` for local. Both `config.js` and `config.local.js` are gitignored.

### Credentials are NOT in `deploy.yaml`
CDK uses the standard AWS credential chain (env vars → `~/.aws/credentials` → SSO). The deploy script must NOT prompt, accept as args, or read from YAML. `aws_account` in YAML is a sanity check only — verify via `boto3.client("sts").get_caller_identity()` before any deploy and fail fast on mismatch.

### Region constraint
`aws_region` must be `"us-east-1"` in v1 — explicit Pydantic validation error otherwise. Required because ACM certs for CloudFront must be in `us-east-1`.

### Secrets injection (v1 strategy)
Both Lambda and EC2 paths fetch SSM Parameter Store values **at deploy time** and inject them as plain env vars (Lambda env vars / systemd `Environment=` directives). Rotating secrets requires redeploy. Runtime fetching is v2.

### Lambda packaging
Use `aws_lambda.Code.from_asset` with bundling that runs `pip install -r requirements.txt -t /asset-output && cp -r . /asset-output` inside `Runtime.PYTHON_3_12.bundling_image`. Docker Desktop must be running locally for this.

### Lambda routing rule
- Single Lambda with `route_prefix: /` → `LambdaRestApi` with `proxy=true` (FastAPI + Mangum pattern).
- Multiple Lambdas with different prefixes → `RestApi` with explicit resources/methods. `route_prefix` values must not overlap (validated in `config_schema.py`).

### EC2 deploy mechanism
CDK uploads the zipped app as an S3 asset. EC2 user data: install Python 3.12, download zip, extract to `/opt/app`, `pip install -r requirements.txt`, write `/etc/systemd/system/app.service` running `app_entrypoint`, `systemctl enable --now`. Redeploys replace the instance — no rolling update logic.

### CloudFront → S3
Use `S3BucketOrigin.with_origin_access_control` (modern OAC), not legacy OAI. S3 bucket is private.

### Route53 hosted zone
Route53 hosted zone must exist before deploy. HostedZone.from_lookup requires the stack to have an explicit env= (account + region) set in app.py — env-agnostic stacks fail the lookup with an unhelpful error. The hosted zone for base_domain must already exist in the target account; the template does not create it.

## Tests

Two test files, both must pass with no AWS credentials and complete in <30s total:

- **`tests/test_config_loader.py`** — config parsing: valid Lambda/EC2 configs load, invalid `backend.type` errors clearly, stage overrides deep-merge correctly, missing required fields name the field, non-`us-east-1` region rejected, overlapping Lambda `route_prefix` rejected.
- **`tests/test_stack_synth.py`** — CDK in-memory synthesis only (`aws_cdk.assertions.Template`). Synthesizes both example stacks, checks expected resource types are present, checks `api_key_required: true` adds `AWS::ApiGateway::ApiKey` + `UsagePlan`, checks `ip_allowlist` populates resource policy CIDRs.

## Pre-commit hooks

`pre-commit install` once after clone. Hooks: standard `pre-commit-hooks` set (incl. `check-added-large-files` max 500KB), `ruff` (format + check), `mypy` strict on `infra/` and `tests/` (excludes `backend/` and `examples/` — those are user app code that travels with each generated project, so type-strictness there would force the template's typing choices on downstream users), `gitleaks` (critical for a deploy tool).

Tooling config in `pyproject.toml`: ruff line length 100, target py312, rules `E,F,I,N,UP,B,SIM`. Mypy strict for `infra/`/`tests/`, ignore missing imports for `aws_cdk.*` if needed.

## CI

`.github/workflows/ci.yml` runs `pre-commit run --all-files` + `pytest` on push/PR. Synthesis-only — no AWS credentials in CI. Real deploys happen from the user's local machine.

## Acceptance criteria reminders (PRD §13)

When implementing, verify against §13 of `PRD.md`. Notable ones easy to miss:
- Both `examples/lambda-minimal` and `examples/ec2-minimal` must deploy end-to-end with only `aws_account` + `base_domain` edits.
- Invalid YAML must produce a Pydantic error naming the offending field **before any AWS call**.
- Credentials/account mismatch must fail fast before any AWS resource calls.
- `dev_server.py` must work on Windows without WSL/Git Bash.