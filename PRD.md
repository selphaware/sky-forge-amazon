# PRD: AWS Deployment Template for Static Frontend + Python Backend

## 1. Overview

Build a reusable AWS deployment template that takes a project (static HTML/JS frontend + Python backend) and deploys it to a custom domain on AWS. The architecture is selected via YAML config — Lambda or EC2 — and all project-specific values (domain, function names, env vars, etc.) come from the same YAML.

The same template must work across multiple projects without modification. To start a new project, the user copies the template, edits `deploy.yaml`, and runs `./scripts/deploy.sh <stage>`.

## 2. Goals & Non-Goals

### Goals
- Single YAML config drives the entire deploy
- Two backend architectures supported: Lambda or EC2 (selected via config)
- Two stages: `dev` and `prod`, with subdomain conventions
- Frontend and backend on the same custom domain (different subdomains)
- HTTPS everywhere, certs auto-managed
- Local development works without AWS — frontend points at localhost backend
- Deploy is idempotent and atomic (CDK / CloudFormation handles this)

### Non-Goals (v1)
- Database provisioning (RDS, DynamoDB, etc.) — out of scope; provision separately
- WAF rules
- Multi-region deployment
- Per-route auth/privacy (v1 applies security at the API level, not per-route)
- Truly VPC-only APIs (only callable from inside a VPC)
- Auto-scaling logic beyond simple min/max counts on EC2
- VPC creation — assume default VPC, or user supplies an existing VPC ID

## 3. Architecture

All architectures share the same frontend setup:
- Static files in **S3**, served via **CloudFront** with HTTPS
- DNS via **Route53**, certs via **ACM**

### 3a. Backend: Lambda
- One or more **Lambda functions**, each with its own source folder, handler, runtime, memory, timeout, env vars, IAM grants
- All Lambdas exposed via a single **API Gateway** (REST API) on `api.<frontend-domain>`
- Each Lambda mapped to a route prefix (e.g. `/users/*`, `/orders/*`, or `/` for a catch-all FastAPI app via Mangum)

### 3b. Backend: EC2
- Single **EC2 instance** of a configurable instance type
- Behind an **Application Load Balancer (ALB)** with HTTPS, on `api.<frontend-domain>`
- App code packaged as a zip, uploaded to S3 by CDK
- EC2 user data installs Python, downloads the zip, extracts, installs `requirements.txt`, runs the app as a systemd service
- Redeploys replace the instance (no rolling update logic in v1)

## 4. Stages and Domain Convention

Two stages: `prod` and `dev`.

For project `<project>` and base domain `<base_domain>`:

| Stage | Frontend                                | Backend API                                  |
|-------|------------------------------------------|-----------------------------------------------|
| prod  | `<project>.<base_domain>`                | `api.<project>.<base_domain>`                 |
| dev   | `<project>.dev.<base_domain>`            | `api.<project>.dev.<base_domain>`             |

Example with `project: newapp`, `base_domain: selpha.com`:
- Prod frontend: `newapp.selpha.com`
- Prod API: `api.newapp.selpha.com`
- Dev frontend: `newapp.dev.selpha.com`
- Dev API: `api.newapp.dev.selpha.com`

The stage is passed as a CLI argument: `./scripts/deploy.sh prod` or `./scripts/deploy.sh dev`. CDK stack names are `<project>-<stage>` (e.g., `newapp-prod`).

## 5. Configuration File: `deploy.yaml`

```yaml
project: newapp
base_domain: selpha.com
aws_account: "123456789012"
aws_region: us-east-1   # must be us-east-1 in v1

frontend:
  source_path: ./frontend
  index_document: index.html
  error_document: error.html   # optional

backend:
  type: lambda   # "lambda" or "ec2"

  # ---- Required if type == lambda ----
  lambdas:
    - name: api
      source_path: ./backend/api
      handler: handler.handler
      runtime: python3.12
      memory: 512
      timeout: 30
      route_prefix: /            # "/" = catch-all (e.g. FastAPI + Mangum)
      env_vars:
        LOG_LEVEL: INFO
      secrets:                   # injected as env vars from SSM Parameter Store
        - name: DB_PASSWORD
          ssm_parameter: /newapp/db_password
      iam_grants:                # additional IAM permissions for this Lambda
        - actions: [s3:GetObject]
          resources: ["arn:aws:s3:::my-bucket/*"]
      vpc:                       # optional — only if Lambda needs VPC access
        vpc_id: vpc-xxx
        subnet_ids: [subnet-xxx, subnet-yyy]
        security_group_ids: [sg-xxx]

  # ---- Required if type == ec2 ----
  ec2:
    instance_type: t3.small
    ami_id: null                 # null = use latest Amazon Linux 2023
    key_name: null               # null = no SSH key (use SSM Session Manager)
    source_path: ./backend/app   # contains requirements.txt and app code
    app_entrypoint: "uvicorn handler:app --host 0.0.0.0 --port 8000"
    app_port: 8000
    health_check_path: /health
    env_vars:
      LOG_LEVEL: INFO
    secrets:
      - name: DB_PASSWORD
        ssm_parameter: /newapp/db_password
    iam_grants:
      - actions: [s3:GetObject]
        resources: ["arn:aws:s3:::my-bucket/*"]
    vpc:                         # optional — uses default VPC if omitted
      vpc_id: vpc-xxx
      public_subnet_ids: [subnet-aaa, subnet-bbb]   # for ALB
      private_subnet_ids: [subnet-ccc, subnet-ddd]  # for EC2 (if private)
    private_subnet: false        # if true, EC2 in private subnet; ALB still public

security:
  cors:
    allowed_origins:
      - "https://newapp.selpha.com"
  api_key_required: false        # if true, API requires x-api-key header
  ip_allowlist: []               # CIDR ranges; empty = no IP restriction

stages:
  prod: {}                       # uses defaults above
  dev:
    security:
      cors:
        allowed_origins: ["*"]   # permissive in dev
      ip_allowlist: []
```

### Config behaviour
- `stages.<stage>` blocks override the top-level keys for that stage (deep merge)
- The deploy script applies stage-specific overrides before passing config to CDK
- Validation via **Pydantic** at load time — fail fast on misconfig with clear errors
- Unknown keys are an error (catches typos)

## 6. Security

### Required behaviours (always applied)
- HTTPS only (HTTP redirects to HTTPS) on both frontend and API
- TLS 1.2+ on CloudFront and ALB
- ACM certs auto-renewed
- Lambda execution role has minimal permissions by default; `iam_grants` adds to it
- EC2 instance role has SSM Session Manager access by default (so SSH key is optional)
- S3 bucket for frontend is private; CloudFront accesses it via Origin Access Control (OAC)

### Configurable security
- **CORS**: `security.cors.allowed_origins` — applied at API Gateway / ALB level
- **API Key**: `security.api_key_required: true` — API Gateway requires `x-api-key`; key is created and the value emitted as a CDK output. For EC2/ALB, key is enforced via a Lambda authorizer is out of scope in v1 — for ALB the API key flag is logged as "not supported in v1, use IP allowlist instead"
- **IP allowlist**: `security.ip_allowlist: [cidr, cidr]` — applied as API Gateway resource policy (Lambda case) or ALB listener rule with security group (EC2 case). Empty list = no restriction
- **VPC**: optional VPC config for Lambda; required for EC2 in private subnet
- **Secrets**: env vars sourced from SSM Parameter Store (SecureString); the IAM role gets read access to those specific parameters automatically

### "Private endpoints" — v1 interpretation
"Private" means the API is not reachable by arbitrary internet clients. In v1 this is achieved by:
1. `api_key_required: true` (anyone with the key can hit it), and/or
2. `ip_allowlist` (only listed CIDRs can hit it)

True VPC-only APIs (PrivateLink / `PRIVATE` API Gateway type) are deferred to v2.

## 6a. AWS Credentials (How CDK Authenticates)

**Critical clarification: AWS credentials are NOT in `deploy.yaml`.** Putting them there would risk committing them to git.

CDK reads credentials from the standard AWS credential chain, in order:
1. Environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optionally `AWS_SESSION_TOKEN`
2. Shared credentials file: `~/.aws/credentials` (populated by `aws configure`)
3. SSO cache: populated by `aws sso login`
4. EC2/ECS instance roles (irrelevant for local deploys)

The user runs `aws configure` (or `aws sso login`) once on their machine. After that, `python scripts/deploy.py <stage>` works without any credential input.

**The deploy script must NOT prompt for credentials, accept them as args, or read them from `deploy.yaml`.** It should just rely on the boto3/CDK default credential chain. If credentials are missing or wrong, CDK itself will produce a clear error — let that propagate.

The `aws_account` field in `deploy.yaml` is used only for stack environment binding (`cdk.Environment(account=..., region=...)`). It's a sanity check, not authentication. If the configured AWS credentials don't match `aws_account`, the deploy should fail with a clear error before any AWS calls are made.

## 6b. API Gateway API Key (the `api_key_required` flag)

This is a separate thing from AWS credentials. When `security.api_key_required: true`:

- CDK creates an API Gateway API Key and Usage Plan as part of the stack
- The key value is **generated by AWS** at deploy time
- After deploy, the key value is emitted as a CDK output (`ApiKey`) and printed by `deploy.py`
- The user copies this key and includes it as the `x-api-key` header in their frontend requests (or curl, etc.)
- The key persists across redeploys; it's only regenerated if the stack is destroyed and recreated

For the frontend to use the key, the deploy script should also write it into `frontend/config.js` alongside `apiUrl`:

```js
window.APP_CONFIG = {
  apiUrl: "https://api.newapp.selpha.com",
  apiKey: "abc123...",   // only present if api_key_required: true
  stage: "prod"
};
```

Frontend code then sends it:
```js
fetch(`${window.APP_CONFIG.apiUrl}/users`, {
  headers: { "x-api-key": window.APP_CONFIG.apiKey }
});
```

**Note:** putting an API key in client-side JS means anyone viewing the page source can see it. This is fine for personal projects and rate-limiting, but it is NOT a real authentication mechanism. Document this limitation in the README.



### Deploy-time generation
Before `cdk deploy` runs, the deploy script generates `config.js` inside the frontend source folder:

```js
// config.js — auto-generated, do not edit
window.APP_CONFIG = {
  apiUrl: "https://api.newapp.selpha.com",
  stage: "prod"
};
```

Frontend HTML loads this before app code:
```html
<script src="config.js"></script>
<script src="app.js"></script>
```

App code references it:
```js
fetch(`${window.APP_CONFIG.apiUrl}/users`)
```

### Local development
- A `config.local.js` (gitignored) sits in the frontend folder for local use:
  ```js
  window.APP_CONFIG = {
    apiUrl: "http://localhost:8000",
    stage: "local"
  };
  ```
- The `dev-server.sh` script copies `config.local.js` → `config.js` and runs:
  - A static file server for the frontend (e.g., `python -m http.server 3000` from the frontend folder)
  - The Python backend locally (`uvicorn handler:app --reload --port 8000`)
- `config.js` is in `.gitignore`; `config.local.js` is also gitignored (it's machine-specific)

### Files to gitignore
```
config.js
config.local.js
.cdk.staging/
cdk.out/
__pycache__/
*.pyc
node_modules/
```

## 8. Repository Structure

```
my-app/
├── deploy.yaml                  # the config
├── frontend/
│   ├── index.html
│   ├── app.js
│   ├── styles.css
│   └── ... (other static assets)
├── backend/
│   ├── api/                     # for Lambda (one folder per Lambda)
│   │   ├── handler.py
│   │   └── requirements.txt
│   └── app/                     # for EC2 (single app)
│       ├── handler.py
│       └── requirements.txt
├── infra/
│   ├── app.py                   # CDK app entry point
│   ├── stacks/
│   │   ├── __init__.py
│   │   ├── lambda_stack.py
│   │   └── ec2_stack.py
│   ├── config_schema.py         # Pydantic models
│   └── config_loader.py         # YAML → validated config + stage merge
├── scripts/
│   ├── deploy.py                # python scripts/deploy.py <stage>
│   ├── destroy.py               # python scripts/destroy.py <stage>
│   └── dev_server.py            # local dev (no AWS calls)
├── examples/
│   ├── lambda-minimal/          # minimal working Lambda example
│   │   ├── deploy.yaml
│   │   ├── frontend/
│   │   │   ├── index.html
│   │   │   ├── app.js
│   │   │   └── config.local.js
│   │   └── backend/api/
│   │       ├── handler.py       # FastAPI + Mangum, one /hello route
│   │       └── requirements.txt
│   └── ec2-minimal/             # minimal working EC2 example
│       ├── deploy.yaml
│       ├── frontend/
│       │   ├── index.html
│       │   ├── app.js
│       │   └── config.local.js
│       └── backend/app/
│           ├── handler.py       # FastAPI + uvicorn, /hello and /health
│           └── requirements.txt
├── cdk.json
├── pyproject.toml               # ruff, mypy, pytest config
├── .pre-commit-config.yaml
├── .github/
│   └── workflows/
│       └── ci.yml
├── tests/
│   ├── __init__.py
│   ├── test_config_loader.py
│   └── test_stack_synth.py
├── requirements.txt             # CDK + Pydantic + PyYAML
├── requirements-dev.txt         # pytest, ruff, mypy, pre-commit
├── .gitignore
└── README.md
```

## 9. Deploy Flow

All scripts are Python (cross-platform, runs natively on Windows). User runs them with the venv activated.

`python scripts/deploy.py <stage>` does:

1. Validate `<stage>` is `prod` or `dev`
2. Load `deploy.yaml`, validate with Pydantic, apply stage overrides
3. Verify configured AWS credentials match `aws_account` (via `boto3.client("sts").get_caller_identity()`); fail fast if not
4. Generate `frontend/config.js` with the resolved API URL for this stage
5. For EC2 path: zip `backend/app` and stage it for upload (use `pathlib` and `zipfile`, not shell commands — works on Windows)
6. Run `cdk deploy <project>-<stage> --require-approval never` via `subprocess.run` with `shell=False`
7. After deploy, parse CDK outputs and print the frontend URL, API URL, and any API key (if generated)

`python scripts/destroy.py <stage>` runs `cdk destroy <project>-<stage>` after a `y/N` prompt confirmation.

`python scripts/dev_server.py` does:
1. Copy `frontend/config.local.js` → `frontend/config.js` (use `shutil.copy`, not `cp`)
2. Start backend in a subprocess: `uvicorn handler:app --reload --port 8000` from the appropriate backend folder
3. Start frontend in a subprocess: `python -m http.server 3000` from the `frontend/` folder
4. Open `http://localhost:3000` in browser via `webbrowser.open`
5. On Ctrl+C, terminate both subprocesses cleanly (use `signal.SIGTERM` on POSIX, `subprocess.Popen.terminate()` on Windows — the latter works cross-platform)

### Windows-specific implementation notes for the scripts
- Use `pathlib.Path` for all file paths, never string concatenation with `/` or `\`
- For subprocess calls, pass `args` as a list, never a single string with `shell=True`
- The `cdk` CLI on Windows is `cdk.cmd` — `subprocess` finds it automatically if the PATH is set, but if invocation fails, fall back to `shutil.which("cdk")` to resolve the full path
- File operations (zip, copy, mkdir) use `shutil` and `zipfile` stdlib modules, not shell commands

## 10. Implementation Notes for Claude Code

### Local environment
- **Platform**: Windows. Python virtual environment is at `env_sky/` in the repo root and activates via `env_sky\Scripts\activate` (cmd) or `env_sky\Scripts\Activate.ps1` (PowerShell). The venv is gitignored.
- All Python commands during implementation/testing must run with this venv activated.
- The PRD's repo structure shows `.sh` scripts in `scripts/`. Because the user is on Windows (no WSL/Git Bash assumed), **convert these to Python scripts** (`deploy.py`, `destroy.py`, `dev_server.py`) instead. Python scripts are cross-platform, run natively in cmd/PowerShell, and are cleaner for orchestration involving YAML parsing and subprocess calls anyway. Invoke as `python scripts/deploy.py <stage>`.

### Tech choices
- **CDK**: `aws-cdk-lib` v2 (Python)
- **Validation**: Pydantic v2
- **YAML**: PyYAML
- **Python**: 3.12+

### CDK specifics
- One `Stack` subclass per architecture: `LambdaStack`, `Ec2Stack`
- `app.py` reads stage from CDK context (`-c stage=prod`), loads config, instantiates the right stack
- Stack name: `f"{project}-{stage}"`
- All resources tagged with `Project=<project>`, `Stage=<stage>`
- Use `Route53.HostedZone.from_lookup` — assumes hosted zone already exists for `base_domain`
- Use `S3BucketOrigin.with_origin_access_control` for CloudFront → S3 (modern OAC, not legacy OAI)
- ACM cert for CloudFront must be in `us-east-1` — assume entire stack deploys there
- Lambda code packaged via `aws_lambda.Code.from_asset` with bundling option:
  ```python
  bundling={
      "image": Runtime.PYTHON_3_12.bundling_image,
      "command": ["bash", "-c", "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output"],
  }
  ```
- Frontend deployed via `s3_deployment.BucketDeployment` with CloudFront invalidation (`distribution_paths=["/*"]`)
- CORS preflight handled by API Gateway's `default_cors_preflight_options` (Lambda) or ALB listener rules (EC2)

### Lambda routing
- If a single Lambda has `route_prefix: /`, use `LambdaRestApi` with `proxy=true`
- If multiple Lambdas with different prefixes, use `RestApi` and add resources/methods explicitly per Lambda

### EC2 user data
- Install Python 3.12, unzip, awscli (preinstalled on AL2023)
- Download app zip from S3 (zip uploaded by CDK as an asset)
- Extract to `/opt/app`, `pip install -r requirements.txt`
- Write a systemd unit `/etc/systemd/system/app.service` that runs `app_entrypoint` from config
- Inject env vars and SSM-sourced secrets into the systemd unit's `Environment=` directives
- `systemctl enable --now app.service`

### Secrets handling
- Lambda: env var sourced from SSM Parameter Store via Lambda's native integration (CDK: `lambda_function.add_environment` won't work for SSM directly — fetch the parameter at deploy time and inject value, OR use Lambda extensions/SSM SDK at runtime). For v1: **fetch at deploy time and inject as plain env var**, simpler. Document that rotating secrets requires redeploy.
- EC2: same approach — fetch at deploy time, inject into systemd unit.
- For v2: runtime fetching via SDK with caching.

### Config validation rules
- `backend.type` is `"lambda"` or `"ec2"` — error otherwise
- If `type == "lambda"`, `backend.lambdas` is required and non-empty
- If `type == "ec2"`, `backend.ec2` is required
- `aws_region` must be `"us-east-1"` in v1 — explicit error otherwise
- Lambda `route_prefix` values must not overlap if multiple Lambdas
- All file paths in config must exist on disk at deploy time

### CDK outputs
After deploy, the stack should output:
- `FrontendUrl` → `https://<frontend-domain>`
- `ApiUrl` → `https://api.<frontend-domain>`
- `ApiKey` → if `api_key_required: true`, the generated key value (CDK can mark this as sensitive)

## 11. Code Quality & CI

### Pre-commit hooks

Configured via `.pre-commit-config.yaml`. The user runs `pre-commit install` once after cloning; hooks then run on every `git commit`.

Required hooks:
- **`pre-commit-hooks`** (standard set): `trailing-whitespace`, `end-of-file-fixer`, `check-yaml`, `check-merge-conflict`, `check-added-large-files` (max 500KB)
- **`ruff`** — Python format + lint, replaces black/isort/flake8. Runs `ruff format` and `ruff check --fix`
- **`mypy`** — Python type checking, strict mode on `infra/` and `tests/`. The `backend/` and `examples/` folders are excluded (user app code, not template code)
- **`gitleaks`** — secrets detection. Critical because this is a deploy tool — accidentally committing AWS keys would be a mess

Frontend (HTML/JS) is not linted in v1 — the template doesn't dictate frontend tooling. Users can add prettier themselves if they want.

### Tooling config (`pyproject.toml`)

- Ruff: line length 100, target Python 3.12, enable rules `E`, `F`, `I`, `N`, `UP`, `B`, `SIM`
- Mypy: strict mode for `infra/` and `tests/`; ignore missing imports for `aws_cdk.*` if needed
- Pytest: `testpaths = ["tests"]`, no special config

### Tests (`tests/`)

Two test files, fast to run, no AWS calls:

**`test_config_loader.py`** — covers config parsing logic:
- Valid Lambda config loads successfully
- Valid EC2 config loads successfully
- Invalid `backend.type` raises a clear Pydantic error
- Stage overrides correctly deep-merge over top-level keys
- Missing required fields produce errors that name the field
- `aws_region != "us-east-1"` is rejected in v1
- Overlapping Lambda `route_prefix` values are rejected

**`test_stack_synth.py`** — covers CDK synthesis:
- `LambdaStack` synthesizes without error using `examples/lambda-minimal/deploy.yaml`
- `Ec2Stack` synthesizes without error using `examples/ec2-minimal/deploy.yaml`
- Synthesized template contains expected resource types (use `aws_cdk.assertions.Template`):
  - Lambda case: `AWS::Lambda::Function`, `AWS::ApiGateway::RestApi`, `AWS::CloudFront::Distribution`, `AWS::S3::Bucket`, `AWS::Route53::RecordSet`, `AWS::CertificateManager::Certificate`
  - EC2 case: `AWS::EC2::Instance`, `AWS::ElasticLoadBalancingV2::LoadBalancer`, plus the same frontend/DNS/cert resources
- `api_key_required: true` adds an `AWS::ApiGateway::ApiKey` and `AWS::ApiGateway::UsagePlan`
- `ip_allowlist` populated → API Gateway has a resource policy with the right CIDRs

These tests must NOT make real AWS calls — they use CDK's in-memory synthesis only. They should run in under 30 seconds total.

### GitHub Actions (`.github/workflows/ci.yml`)

Single workflow, runs on push to any branch and on PRs:

```yaml
name: CI
on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: actions/setup-node@v4
        with:
          node-version: "24"
      - run: npm install -g aws-cdk
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pre-commit run --all-files
      - run: pytest
```

No deploy step, no AWS credentials needed in CI — synthesis-only validation. The user pushes to GitHub manually; the workflow runs and reports pass/fail. Real deploys happen from the user's local machine via `./scripts/deploy.sh`.

## 12. Prerequisites (for the user, not Claude Code to implement)

- **Platform**: Windows (no WSL/Git Bash assumed; runs in cmd or PowerShell)
- Python 3.12+ in a venv at `env_sky/` (activate via `env_sky\Scripts\activate`)
- AWS CLI configured with credentials for `aws_account` (`aws configure` or `aws sso login`)
- `cdk bootstrap aws://<account>/us-east-1` already run on target account
- Route53 hosted zone exists for `base_domain`
- Node.js 20+ for `cdk` CLI (tested with Node 24.15.0, npm 11.14.0)
- Docker Desktop running (for Lambda asset bundling during `cdk deploy`)

## 13. Acceptance Criteria

A working implementation must satisfy:

1. With a fresh clone of the template and a valid `deploy.yaml` (Lambda variant), `python scripts/deploy.py dev` produces a working dev environment at `<project>.dev.<base_domain>` with the API at `api.<project>.dev.<base_domain>`.
2. The frontend can call the API successfully (CORS works, HTTPS works).
3. Switching `deploy.yaml` to the EC2 variant and redeploying produces an equivalent EC2-backed environment.
4. `python scripts/deploy.py prod` deploys to the prod URLs without affecting the dev stack.
5. `python scripts/dev_server.py` runs the same frontend and backend locally with `localhost:3000` → `localhost:8000`, no AWS calls required, on Windows without WSL or Git Bash.
6. `python scripts/destroy.py dev` cleanly tears down the dev stack.
7. Invalid YAML produces a clear Pydantic error with the offending field, before any AWS call is made.
8. Setting `api_key_required: true` results in unauthenticated API calls returning 403; calls with the correct `x-api-key` header succeed. The key value appears in `config.js` and is emitted as a CDK output.
9. Setting `ip_allowlist: ["1.2.3.4/32"]` blocks all other IPs.
10. Both `examples/lambda-minimal` and `examples/ec2-minimal` deploy end-to-end with no manual edits beyond setting `aws_account` and `base_domain` in their respective `deploy.yaml` files.
11. The deploy script does not accept credentials as args, prompt for them, or read them from `deploy.yaml`. Credentials come from the standard AWS credential chain only.
12. If the configured AWS credentials don't match `aws_account` in `deploy.yaml`, the deploy fails with a clear error before making AWS resource calls.
13. `pre-commit run --all-files` passes on a fresh clone after `pip install -r requirements-dev.txt && pre-commit install`.
14. `pytest` passes with no AWS credentials configured, running in under 30 seconds.
15. The GitHub Actions workflow (`pre-commit` + `pytest`) passes on push.

## 14. Future (v2+) — Not in scope

- Per-route auth (some routes public, some private)
- True VPC-only APIs (PrivateLink)
- Multi-region deploys
- Database provisioning constructs
- ECS/Fargate as a third backend type
- Runtime secret fetching with rotation
- Custom domains with non-Route53 DNS
- Multiple stages beyond dev/prod (e.g., staging, qa)