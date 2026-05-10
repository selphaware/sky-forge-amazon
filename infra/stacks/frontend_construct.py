"""Shared frontend construct: S3 + CloudFront (OAC) + ACM cert + Route53 + BucketDeployment.

Used by both LambdaStack and Ec2Stack. Domain-naming helpers are module-level
so any stack can compute subdomain strings without instantiating a Construct.
"""

from __future__ import annotations

from aws_cdk import RemovalPolicy
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3_deployment
from constructs import Construct

from infra.config_schema import DeployConfig


def frontend_domain(cfg: DeployConfig) -> str:
    """Return the fully-qualified frontend hostname for the given config+stage."""
    if cfg.stage == "prod":
        return f"{cfg.project}.{cfg.base_domain}"
    return f"{cfg.project}.dev.{cfg.base_domain}"


def record_name(project: str, stage: str, prefix: str = "") -> str:
    """Return the Route53 record name (relative to the hosted zone's base domain)."""
    if stage == "prod":
        return f"{prefix}{project}"
    return f"{prefix}{project}.dev"


class FrontendConstruct(Construct):
    """Builds the complete static-frontend leg: private S3 bucket served via
    CloudFront with modern OAC, TLSv1.2_2021 minimum, ACM cert, and Route53 alias.

    Public attributes
    -----------------
    bucket : s3.Bucket
    distribution : cloudfront.Distribution
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: DeployConfig,
        zone: route53.IHostedZone,
    ) -> None:
        super().__init__(scope, construct_id)

        domain = frontend_domain(cfg)

        self.bucket = s3.Bucket(
            self,
            "FrontendBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
            enforce_ssl=True,
        )

        cert = acm.Certificate(
            self,
            "FrontendCert",
            domain_name=domain,
            validation=acm.CertificateValidation.from_dns(zone),
        )

        error_responses: list[cloudfront.ErrorResponse] = []
        if cfg.frontend.error_document:
            for status in (403, 404):
                error_responses.append(
                    cloudfront.ErrorResponse(
                        http_status=status,
                        response_http_status=status,
                        response_page_path=f"/{cfg.frontend.error_document}",
                    )
                )

        self.distribution = cloudfront.Distribution(
            self,
            "FrontendDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            domain_names=[domain],
            certificate=cert,
            default_root_object=cfg.frontend.index_document,
            error_responses=error_responses or None,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )

        s3_deployment.BucketDeployment(
            self,
            "FrontendDeployment",
            sources=[s3_deployment.Source.asset(cfg.frontend.source_path)],
            destination_bucket=self.bucket,
            distribution=self.distribution,
            distribution_paths=["/*"],
        )

        route53.ARecord(
            self,
            "FrontendRecord",
            zone=zone,
            record_name=record_name(cfg.project, cfg.stage),
            target=route53.RecordTarget.from_alias(
                route53_targets.CloudFrontTarget(self.distribution)
            ),
        )
