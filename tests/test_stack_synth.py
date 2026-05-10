"""CDK synth tests for Lambda and EC2 stacks.

In-memory only — no AWS credentials, no Docker. Stacks accept injected
``hosted_zone`` (from_hosted_zone_attributes, no lookup) and fake VPCs.
Lambda stacks take ``bundle_lambdas=False`` to skip Docker asset bundling.
"""

from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_route53 as route53
from aws_cdk.assertions import Annotations as StackAnnotations  # noqa: N814
from aws_cdk.assertions import Match, Template

from infra.config_loader import load_config
from infra.config_schema import DeployConfig, Ec2Backend, LambdaBackend, SecretConfig
from infra.stacks.ec2_stack import Ec2Stack
from infra.stacks.lambda_stack import LambdaStack

REPO_ROOT = Path(__file__).resolve().parent.parent
LAMBDA_MINIMAL = REPO_ROOT / "examples" / "lambda-minimal"
EC2_MINIMAL = REPO_ROOT / "examples" / "ec2-minimal"


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


# =========================================================================== #
# EC2 STACK TESTS                                                             #
# =========================================================================== #


def _build_ec2(cfg: DeployConfig) -> tuple[cdk.Stack, Template]:
    app = cdk.App()
    env = cdk.Environment(account=cfg.aws_account, region=cfg.aws_region)

    fake_zone = route53.HostedZone.from_hosted_zone_attributes(
        app, "FakeZone", hosted_zone_id="Z123FAKE", zone_name=cfg.base_domain
    )
    # ec2.Vpc.from_vpc_attributes requires a Stack scope (not App). Use a throwaway
    # helper Stack so CDK has account+region context for the imported VPC. Since
    # from_vpc_attributes creates no CFN resources, no cross-stack exports are emitted.
    vpc_scope = cdk.Stack(app, "VpcHelper", env=env)
    fake_vpc = ec2.Vpc.from_vpc_attributes(
        vpc_scope,
        "FakeVpc",
        vpc_id="vpc-12345",
        availability_zones=["us-east-1a", "us-east-1b"],
        public_subnet_ids=["subnet-pub-1", "subnet-pub-2"],
    )
    stack = Ec2Stack(
        app,
        f"{cfg.project}-{cfg.stage}",
        cfg=cfg,
        hosted_zone=fake_zone,
        vpc=fake_vpc,
        env=env,
    )
    return stack, Template.from_stack(stack)


@pytest.fixture
def cfg_ec2_prod() -> DeployConfig:
    cfg = load_config(EC2_MINIMAL / "deploy.yaml", "prod", validate_paths=True)
    assert isinstance(cfg.backend, Ec2Backend)
    # Avoid latest_amazon_linux2023() context lookup during synth.
    cfg.backend.ec2.ami_id = "ami-12345fake"
    return cfg


# --------------------------------------------------------------------------- #
# Resource counts                                                             #
# --------------------------------------------------------------------------- #


def test_ec2_stack_synthesizes(cfg_ec2_prod: DeployConfig) -> None:
    _, template = _build_ec2(cfg_ec2_prod)
    # Instance lives in the ASG / LaunchTemplate, not a bare AWS::EC2::Instance.
    template.resource_count_is("AWS::AutoScaling::AutoScalingGroup", 1)
    template.resource_count_is("AWS::EC2::LaunchTemplate", 1)
    template.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 1)
    template.resource_count_is("AWS::ElasticLoadBalancingV2::TargetGroup", 1)
    # HTTPS listener + HTTP redirect listener
    template.resource_count_is("AWS::ElasticLoadBalancingV2::Listener", 2)
    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    template.resource_count_is("AWS::Route53::RecordSet", 2)
    template.resource_count_is("AWS::CertificateManager::Certificate", 2)


# --------------------------------------------------------------------------- #
# ip_allowlist → ALB security group                                           #
# --------------------------------------------------------------------------- #


def test_ec2_ip_allowlist_in_security_group(cfg_ec2_prod: DeployConfig) -> None:
    cfg = cfg_ec2_prod.model_copy(deep=True)
    cfg.security.ip_allowlist = ["1.2.3.4/32", "5.6.7.0/24"]
    _, template = _build_ec2(cfg)
    rendered = json.dumps(template.to_json())
    assert "1.2.3.4/32" in rendered
    assert "5.6.7.0/24" in rendered
    # The security group allowing those CIDRs must exist.
    template.has_resource_properties(
        "AWS::EC2::SecurityGroup",
        Match.object_like(
            {
                "SecurityGroupIngress": Match.array_with(
                    [Match.object_like({"CidrIp": "1.2.3.4/32"})]
                )
            }
        ),
    )


def test_ec2_ip_allowlist_empty_opens_public(cfg_ec2_prod: DeployConfig) -> None:
    _, template = _build_ec2(cfg_ec2_prod)
    rendered = json.dumps(template.to_json())
    assert "0.0.0.0/0" in rendered


# --------------------------------------------------------------------------- #
# CloudFront posture mirrors Lambda                                            #
# --------------------------------------------------------------------------- #


def test_ec2_no_origin_access_identity(cfg_ec2_prod: DeployConfig) -> None:
    _, template = _build_ec2(cfg_ec2_prod)
    template.resource_count_is("AWS::CloudFront::CloudFrontOriginAccessIdentity", 0)


def test_ec2_cloudfront_tls_minimum(cfg_ec2_prod: DeployConfig) -> None:
    _, template = _build_ec2(cfg_ec2_prod)
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
# Tags                                                                         #
# --------------------------------------------------------------------------- #


def test_ec2_resources_have_project_tag(cfg_ec2_prod: DeployConfig) -> None:
    _, template = _build_ec2(cfg_ec2_prod)
    expected_tag = {"Key": "Project", "Value": cfg_ec2_prod.project}
    # ASG propagates tags to the EC2 instance at launch.
    template.has_resource_properties(
        "AWS::AutoScaling::AutoScalingGroup",
        Match.object_like({"Tags": Match.array_with([Match.object_like({"Key": "Project"})])}),
    )
    template.has_resource_properties(
        "AWS::S3::Bucket",
        Match.object_like({"Tags": Match.array_with([Match.object_like(expected_tag)])}),
    )


# --------------------------------------------------------------------------- #
# Secrets — dynamic references in user data                                   #
# --------------------------------------------------------------------------- #


def test_ec2_no_plaintext_secrets_in_template(cfg_ec2_prod: DeployConfig) -> None:
    """User data must use CFN dynamic refs, never embed plaintext secret values."""
    # ec2-minimal deploy.yaml already declares DB_PASSWORD; it will be in sub_vars.
    _, template = _build_ec2(cfg_ec2_prod)
    rendered = json.dumps(template.to_json())
    assert "{{resolve:secretsmanager:" in rendered
    assert "ec2-minimal/prod/db_password" in rendered


def test_ec2_user_data_uses_dynamic_references(cfg_ec2_prod: DeployConfig) -> None:
    """Every secret Environment= line in the Fn::Sub template string must reference
    a ${Secret_*} variable, not a literal value. Catches the failure mode where
    Fn::Sub is removed and secrets silently become empty strings."""
    _, template = _build_ec2(cfg_ec2_prod)
    rendered = json.dumps(template.to_json())
    # The Fn::Sub template string contains the variable reference.
    assert "${Secret_DB_PASSWORD}" in rendered
    # And the Environment= line uses that reference, not a literal.
    assert "Environment=DB_PASSWORD=${Secret_DB_PASSWORD}" in rendered


# --------------------------------------------------------------------------- #
# api_key_required for EC2 → warning, no ApiKey/UsagePlan resources           #
# --------------------------------------------------------------------------- #


def test_ec2_api_key_required_logs_warning(cfg_ec2_prod: DeployConfig) -> None:
    cfg = cfg_ec2_prod.model_copy(deep=True)
    cfg.security.api_key_required = True
    stack, template = _build_ec2(cfg)

    # No API Gateway key resources should be synthesized.
    template.resource_count_is("AWS::ApiGateway::ApiKey", 0)
    template.resource_count_is("AWS::ApiGateway::UsagePlan", 0)

    # A CDK annotation warning must be present somewhere in the stack.
    annotations = StackAnnotations.from_stack(stack)
    annotations.has_warning("*", Match.string_like_regexp("api_key_required"))
