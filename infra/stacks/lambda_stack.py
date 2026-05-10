"""LambdaStack: CDK stack for the Lambda backend variant."""

from __future__ import annotations

from typing import Any

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    SecretValue,
    Stack,
    Tags,
)
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from infra.config_schema import DeployConfig, LambdaBackend, LambdaConfig
from infra.stacks.frontend_construct import FrontendConstruct, frontend_domain, record_name


class LambdaStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: DeployConfig,
        hosted_zone: route53.IHostedZone | None = None,
        bundle_lambdas: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if not isinstance(cfg.backend, LambdaBackend):
            raise TypeError(
                f"LambdaStack requires backend.type='lambda' (got {cfg.backend.type!r})"
            )

        Tags.of(self).add("Project", cfg.project)
        Tags.of(self).add("Stage", cfg.stage)

        fe_domain = frontend_domain(cfg)
        api_domain = f"api.{fe_domain}"

        zone = (
            hosted_zone
            if hosted_zone is not None
            else route53.HostedZone.from_lookup(self, "HostedZone", domain_name=cfg.base_domain)
        )

        FrontendConstruct(self, "Frontend", cfg=cfg, zone=zone)
        self._build_api(cfg, api_domain, zone, bundle_lambdas)

        CfnOutput(self, "FrontendUrl", value=f"https://{fe_domain}")
        CfnOutput(self, "ApiUrl", value=f"https://{api_domain}")

    # ------------------------------------------------------------------ #
    # API                                                                #
    # ------------------------------------------------------------------ #

    def _build_api(
        self,
        cfg: DeployConfig,
        api_domain: str,
        zone: route53.IHostedZone,
        bundle_lambdas: bool,
    ) -> None:
        assert isinstance(cfg.backend, LambdaBackend)

        api_cert = acm.Certificate(
            self,
            "ApiCert",
            domain_name=api_domain,
            validation=acm.CertificateValidation.from_dns(zone),
        )

        api_domain_name = apigateway.DomainName(
            self,
            "ApiDomain",
            domain_name=api_domain,
            certificate=api_cert,
            endpoint_type=apigateway.EndpointType.REGIONAL,
        )

        funcs: list[tuple[LambdaConfig, lambda_.Function]] = []
        for lam in cfg.backend.lambdas:
            fn = lambda_.Function(
                self,
                f"Fn-{lam.name}",
                function_name=f"{cfg.project}-{cfg.stage}-{lam.name}",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=lam.handler,
                code=self._make_code(lam.source_path, bundle=bundle_lambdas),
                memory_size=lam.memory,
                timeout=Duration.seconds(lam.timeout),
            )
            for k, v in lam.env_vars.items():
                fn.add_environment(k, v)
            for grant in lam.iam_grants:
                fn.add_to_role_policy(
                    iam.PolicyStatement(actions=grant.actions, resources=grant.resources)
                )
            for sec in lam.secrets:
                assert sec.aws_secret_name is not None
                sm = secretsmanager.Secret.from_secret_name_v2(
                    self, f"Sec-{lam.name}-{sec.name}", sec.aws_secret_name
                )
                fn.add_environment(
                    sec.name,
                    SecretValue.secrets_manager(sec.aws_secret_name).unsafe_unwrap(),
                )
                if fn.role is not None:
                    sm.grant_read(fn.role)
            funcs.append((lam, fn))

        cors_opts: apigateway.CorsOptions | None = None
        if cfg.security.cors.allowed_origins:
            cors_opts = apigateway.CorsOptions(
                allow_origins=cfg.security.cors.allowed_origins,
                allow_methods=apigateway.Cors.ALL_METHODS,
                allow_headers=apigateway.Cors.DEFAULT_HEADERS,
            )
        method_opts = apigateway.MethodOptions(api_key_required=cfg.security.api_key_required)

        policy_doc: iam.PolicyDocument | None = None
        if cfg.security.ip_allowlist:
            policy_doc = iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        effect=iam.Effect.ALLOW,
                        principals=[iam.AnyPrincipal()],
                        actions=["execute-api:Invoke"],
                        resources=["execute-api:/*"],
                    ),
                    iam.PolicyStatement(
                        effect=iam.Effect.DENY,
                        principals=[iam.AnyPrincipal()],
                        actions=["execute-api:Invoke"],
                        resources=["execute-api:/*"],
                        conditions={
                            "NotIpAddress": {"aws:SourceIp": cfg.security.ip_allowlist},
                        },
                    ),
                ]
            )

        single_catchall = len(funcs) == 1 and funcs[0][0].route_prefix == "/"
        rest_api: apigateway.RestApi
        if single_catchall:
            rest_api = apigateway.LambdaRestApi(
                self,
                "RestApi",
                handler=funcs[0][1],
                proxy=True,
                rest_api_name=f"{cfg.project}-{cfg.stage}",
                default_cors_preflight_options=cors_opts,
                default_method_options=method_opts,
                policy=policy_doc,
            )
        else:
            rest_api = apigateway.RestApi(
                self,
                "RestApi",
                rest_api_name=f"{cfg.project}-{cfg.stage}",
                default_cors_preflight_options=cors_opts,
                default_method_options=method_opts,
                policy=policy_doc,
            )
            for lam, fn in funcs:
                resource: apigateway.IResource = rest_api.root
                for segment in lam.route_prefix.strip("/").split("/"):
                    if segment:
                        resource = resource.add_resource(segment)
                resource.add_method("ANY", apigateway.LambdaIntegration(fn))
                resource.add_proxy(
                    default_integration=apigateway.LambdaIntegration(fn),
                    any_method=True,
                )

        api_domain_name.add_base_path_mapping(rest_api)

        route53.ARecord(
            self,
            "ApiRecord",
            zone=zone,
            record_name=record_name(cfg.project, cfg.stage, prefix="api."),
            target=route53.RecordTarget.from_alias(
                route53_targets.ApiGatewayDomain(api_domain_name)
            ),
        )

        if cfg.security.api_key_required:
            api_key = apigateway.ApiKey(
                self,
                "ApiKey",
                api_key_name=f"{cfg.project}-{cfg.stage}-key",
            )
            usage_plan = apigateway.UsagePlan(
                self,
                "UsagePlan",
                name=f"{cfg.project}-{cfg.stage}-usage",
                api_stages=[
                    apigateway.UsagePlanPerApiStage(api=rest_api, stage=rest_api.deployment_stage),
                ],
            )
            usage_plan.add_api_key(api_key)
            CfnOutput(
                self,
                "ApiKeyId",
                value=api_key.key_id,
                description=(
                    "API Gateway API Key ID. deploy.py retrieves the value via "
                    "apigateway.get_api_key(apiKey=<id>, includeValue=True)."
                ),
            )

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _make_code(source_path: str, *, bundle: bool) -> lambda_.Code:
        if bundle:
            return lambda_.Code.from_asset(
                source_path,
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install -r requirements.txt -t /asset-output && cp -r . /asset-output",
                    ],
                ),
            )
        return lambda_.Code.from_asset(source_path)
