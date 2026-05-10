"""CDK synth tests for the Lambda stack.

In-memory only — no AWS credentials, no Docker. The stack accepts an injected
``hosted_zone`` (built via from_hosted_zone_attributes, no AWS lookup) and a
``bundle_lambdas=False`` flag so Code.from_asset skips Docker.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk import aws_route53 as route53
from aws_cdk.assertions import Match, Template

from infra.config_loader import load_config
from infra.config_schema import DeployConfig, LambdaBackend, SecretConfig
from infra.stacks.lambda_stack import LambdaStack

REPO_ROOT = Path(__file__).resolve().parent.parent
LAMBDA_MINIMAL = REPO_ROOT / "examples" / "lambda-minimal"


def _build(cfg: DeployConfig) -> Template:
    app = cdk.App()
    fake_zone = route53.HostedZone.from_hosted_zone_attributes(
        app, "FakeZone", hosted_zone_id="Z123FAKE", zone_name=cfg.base_domain
    )
    stack = LambdaStack(
        app,
        f"{cfg.project}-{cfg.stage}",
        cfg=cfg,
        hosted_zone=fake_zone,
        bundle_lambdas=False,
        env=cdk.Environment(account=cfg.aws_account, region=cfg.aws_region),
    )
    return Template.from_stack(stack)


@pytest.fixture
def cfg_prod() -> DeployConfig:
    return load_config(LAMBDA_MINIMAL / "deploy.yaml", "prod", validate_paths=True)


@pytest.fixture
def cfg_dev() -> DeployConfig:
    return load_config(LAMBDA_MINIMAL / "deploy.yaml", "dev", validate_paths=True)


# --------------------------------------------------------------------------- #
# Resource counts                                                             #
# --------------------------------------------------------------------------- #


def test_lambda_stack_synthesizes(cfg_prod: DeployConfig) -> None:
    template = _build(cfg_prod)
    # Assert presence of OUR user Lambda by function_name, not raw count — CDK's
    # BucketDeployment construct synthesizes its own helper Lambda, so a strict
    # count check would be brittle.
    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like({"FunctionName": "lambda-minimal-prod-api"}),
    )
    template.resource_count_is("AWS::ApiGateway::RestApi", 1)
    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    template.has_resource("AWS::S3::Bucket", {})
    # Two ARecords: frontend + api.
    template.resource_count_is("AWS::Route53::RecordSet", 2)
    # Two ACM certs: frontend + api.
    template.resource_count_is("AWS::CertificateManager::Certificate", 2)


def test_lambda_stack_synthesizes_dev(cfg_dev: DeployConfig) -> None:
    template = _build(cfg_dev)
    # dev override sets cors.allowed_origins=["*"]
    rendered = json.dumps(template.to_json())
    assert '"*"' in rendered
    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like({"FunctionName": "lambda-minimal-dev-api"}),
    )


# --------------------------------------------------------------------------- #
# api_key_required                                                            #
# --------------------------------------------------------------------------- #


def test_api_key_required_adds_resources(cfg_prod: DeployConfig) -> None:
    cfg = cfg_prod.model_copy(deep=True)
    cfg.security.api_key_required = True
    template = _build(cfg)
    template.resource_count_is("AWS::ApiGateway::ApiKey", 1)
    template.resource_count_is("AWS::ApiGateway::UsagePlan", 1)


def test_api_key_not_required_omits_resources(cfg_prod: DeployConfig) -> None:
    template = _build(cfg_prod)
    template.resource_count_is("AWS::ApiGateway::ApiKey", 0)
    template.resource_count_is("AWS::ApiGateway::UsagePlan", 0)


# --------------------------------------------------------------------------- #
# ip_allowlist                                                                #
# --------------------------------------------------------------------------- #


def test_ip_allowlist_in_resource_policy(cfg_prod: DeployConfig) -> None:
    cfg = cfg_prod.model_copy(deep=True)
    cfg.security.ip_allowlist = ["1.2.3.4/32", "5.6.7.0/24"]
    template = _build(cfg)
    template.has_resource_properties(
        "AWS::ApiGateway::RestApi",
        Match.object_like({"Policy": Match.any_value()}),
    )
    rendered = json.dumps(template.to_json())
    assert "1.2.3.4/32" in rendered
    assert "5.6.7.0/24" in rendered


def test_ip_allowlist_empty_omits_policy(cfg_prod: DeployConfig) -> None:
    template = _build(cfg_prod)
    rest_apis = template.find_resources("AWS::ApiGateway::RestApi")
    assert len(rest_apis) == 1
    props = next(iter(rest_apis.values()))["Properties"]
    assert "Policy" not in props


# --------------------------------------------------------------------------- #
# Secrets — PRD acceptance criterion #21                                      #
# --------------------------------------------------------------------------- #


def test_no_plaintext_secrets_in_template(cfg_prod: DeployConfig) -> None:
    """Template contains only {{resolve:secretsmanager:...}} dynamic refs, never plaintext.

    Adds a synthetic SecretConfig to the resolved cfg (post-validation) so we have
    a known aws_secret_name to find in the rendered template.
    """
    cfg = cfg_prod.model_copy(deep=True)
    assert isinstance(cfg.backend, LambdaBackend)
    aws_secret_name = f"{cfg.project}/{cfg.stage}/db_password"
    cfg.backend.lambdas[0].secrets.append(
        SecretConfig(
            name="DB_PASSWORD",
            prompt="DB_PASSWORD",
            description=f"DB_PASSWORD for {cfg.project} ({cfg.stage})",
            aws_secret_name=aws_secret_name,
        )
    )
    template = _build(cfg)
    rendered = json.dumps(template.to_json())
    assert "{{resolve:secretsmanager:" in rendered
    assert aws_secret_name in rendered


# --------------------------------------------------------------------------- #
# CORS — empty origins should not emit preflight                              #
# --------------------------------------------------------------------------- #


def test_cors_empty_origins_omits_preflight(cfg_prod: DeployConfig) -> None:
    """When allowed_origins is empty, no OPTIONS preflight method is emitted."""
    cfg = cfg_prod.model_copy(deep=True)
    cfg.security.cors.allowed_origins = []
    template = _build(cfg)
    methods = template.find_resources("AWS::ApiGateway::Method")
    options_methods = [
        v for v in methods.values() if v["Properties"].get("HttpMethod") == "OPTIONS"
    ]
    assert options_methods == []


def test_cors_origins_emits_preflight(cfg_prod: DeployConfig) -> None:
    template = _build(cfg_prod)
    methods = template.find_resources("AWS::ApiGateway::Method")
    options_methods = [
        v for v in methods.values() if v["Properties"].get("HttpMethod") == "OPTIONS"
    ]
    assert len(options_methods) >= 1


# --------------------------------------------------------------------------- #
# CloudFront posture: modern OAC, no legacy OAI, TLS 1.2_2021 minimum         #
# --------------------------------------------------------------------------- #


def test_no_origin_access_identity(cfg_prod: DeployConfig) -> None:
    """The S3 origin must use modern OAC (S3BucketOrigin.with_origin_access_control),
    never the legacy CloudFrontOriginAccessIdentity."""
    template = _build(cfg_prod)
    template.resource_count_is("AWS::CloudFront::CloudFrontOriginAccessIdentity", 0)


def test_cloudfront_tls_minimum(cfg_prod: DeployConfig) -> None:
    """The CloudFront distribution must require TLSv1.2_2021 at the viewer."""
    template = _build(cfg_prod)
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        Match.object_like(
            {
                "DistributionConfig": Match.object_like(
                    {
                        "ViewerCertificate": Match.object_like(
                            {"MinimumProtocolVersion": "TLSv1.2_2021"}
                        ),
                    }
                ),
            }
        ),
    )


# --------------------------------------------------------------------------- #
# Tags propagate from Stack root to child resources                           #
# --------------------------------------------------------------------------- #


def test_resources_have_project_tag(cfg_prod: DeployConfig) -> None:
    """Tags.of(self).add('Project', ...) at the stack root should propagate to
    taggable resources. Spot-check the user Lambda and the frontend bucket."""
    template = _build(cfg_prod)
    expected_tag = {"Key": "Project", "Value": cfg_prod.project}

    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like(
            {
                "FunctionName": f"{cfg_prod.project}-{cfg_prod.stage}-api",
                "Tags": Match.array_with([Match.object_like(expected_tag)]),
            }
        ),
    )
    template.has_resource_properties(
        "AWS::S3::Bucket",
        Match.object_like({"Tags": Match.array_with([Match.object_like(expected_tag)])}),
    )
