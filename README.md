# sky-forge-amazon

A reusable AWS deployment template for projects with a static HTML/JS frontend and a Python backend. A single `deploy.yaml` selects between two backend architectures (Lambda or EC2), and the scripts handle the rest: secrets bootstrapping, CDK synthesis, stack deployment, and local development.

See `PRD.md` for the full design document and acceptance criteria.

---

## Prerequisites

- Python 3.12+ in a venv at `env_sky/` (create with `python -m venv env_sky`)
- Node.js 20+ for the CDK CLI (`npm install -g aws-cdk`)
- AWS CLI configured for your target account (`aws configure` or `aws sso login`)
- CDK bootstrapped in `us-east-1`: `cdk bootstrap aws://<account>/us-east-1`
- Docker Desktop running (required for Lambda asset bundling during `cdk deploy`)
- A Route53 hosted zone for your `base_domain` already exists in the target account

All scripts run natively on Windows (cmd or PowerShell). No WSL or Git Bash required.

---

## Quickstart

```powershell
git clone <repo>
cd sky-forge-amazon
python -m venv env_sky
env_sky\Scripts\Activate.ps1   # or: env_sky\Scripts\activate (cmd)
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install
```

Copy `examples/lambda-minimal/` into your project directory, edit `deploy.yaml` to set `aws_account` and `base_domain`, then:

```powershell
python scripts/deploy.py dev
```

The first deploy prompts for any declared secrets (input is not echoed), creates them in AWS Secrets Manager, then runs `cdk deploy`.

---

## Scripts

| Script | What it does |
|--------|--------------|
| `scripts/deploy.py <stage>` | Full deploy: verify credentials, bootstrap secrets, generate `config.js`, run `cdk deploy`, print outputs |
| `scripts/destroy.py <stage>` | Destroy the stack after y/N confirmation (secrets are not deleted) |
| `scripts/set_secret.py <stage> <name>` | Rotate a secret (prompts for new value); add `--delete` to schedule deletion |
| `scripts/dev_server.py` | Local dev: copies `config.local.js` → `config.js`, starts uvicorn + http.server, opens browser |

All subprocess calls use `shell=False`. All paths use `pathlib.Path`. See PRD §9 for the full deploy flow.

---

## Secrets

Secrets are declared by name in `deploy.yaml` under `secrets:` — values are never in YAML or git. On first deploy, `deploy.py` prompts for each missing secret via `getpass` (input not echoed) and creates it in AWS Secrets Manager. Subsequent deploys skip existing secrets.

Default secret name: `<project>/<stage>/<name.lower()>`. Prod and dev get separate secrets by default; override `aws_secret_name` to share.

CFN resolves secrets server-side via `{{resolve:secretsmanager:...}}` dynamic references at stack create/update time. No plaintext appears in the synthesized template.

**Local dev does not inject secrets.** Handle local values yourself — for example, a gitignored `.env` file in the backend directory, or values guarded by `if os.getenv("STAGE") == "local":`. See PRD §6c.

---

## Stages and domains

| Stage | Frontend | API |
|-------|----------|-----|
| `prod` | `<project>.<base_domain>` | `api.<project>.<base_domain>` |
| `dev` | `<project>.dev.<base_domain>` | `api.<project>.dev.<base_domain>` |

Stage overrides are deep-merged: `stages.dev:` in `deploy.yaml` overrides top-level keys for that stage. Lists are replaced wholesale (not appended).

Stack names are `<project>-<stage>`. Running `deploy.py prod` and `deploy.py dev` produces two independent stacks.

---

## Local development

```powershell
python scripts/dev_server.py
```

No AWS calls. Frontend serves on port 3000, backend on port 8000. Reads `deploy.yaml` to find the backend source directory, then starts uvicorn with `--reload`.

Create `frontend/config.local.js` pointing at `http://localhost:8000` — it is gitignored and machine-specific:

```js
window.APP_CONFIG = { apiUrl: "http://localhost:8000", stage: "local" };
```

---

## Testing

```powershell
pytest          # 82 tests, <30s, no AWS credentials needed
pytest -k "stage_merge" -v   # single test
```

Tests cover config validation, CDK synthesis (in-memory, no Docker), and script unit logic. No real AWS calls are made in the test suite.

```powershell
pre-commit run --all-files   # ruff, mypy strict, gitleaks, standard hooks
```

Run `pre-commit install` once after cloning.

---

## Limitations (v1)

The following are out of scope for v1. See PRD §14.

- Database provisioning (RDS, DynamoDB, etc.)
- WAF rules
- Multi-region deployment
- True VPC-only (PrivateLink) APIs
- ECS/Fargate backend
- Runtime secret fetching — secrets are fetched at deploy time; rotation requires redeploy
- Custom domains with non-Route53 DNS
- Multiple stages beyond `dev` and `prod`
- `aws_region` other than `us-east-1` (ACM certs for CloudFront must be in us-east-1)
