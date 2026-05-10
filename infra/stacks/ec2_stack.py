"""Ec2Stack: CDK stack for the EC2 backend variant.

Backend: single EC2 instance (min=max=1 AutoScalingGroup) behind an ALB.
Replacement update policy ensures redeploys replace the instance rather than
updating in-place (which would leave stale user data on the old instance).

Secrets are injected into the systemd unit via CFN Fn::Sub dynamic references —
CFN resolves {{resolve:secretsmanager:...}} before base64-encoding user data,
so the EC2 instance receives plain values in its Environment= directives.
"""

from __future__ import annotations

from typing import Any, cast

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Stack,
    Tags,
)
from aws_cdk import aws_autoscaling as autoscaling
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from infra.config_schema import DeployConfig, Ec2Backend
from infra.stacks.frontend_construct import FrontendConstruct, frontend_domain, record_name


class Ec2Stack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: DeployConfig,
        hosted_zone: route53.IHostedZone | None = None,
        vpc: ec2.IVpc | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if not isinstance(cfg.backend, Ec2Backend):
            raise TypeError(f"Ec2Stack requires backend.type='ec2' (got {cfg.backend.type!r})")

        # api_key_required is not supported for EC2 in v1: ALB has no API key
        # mechanism. Log a CDK-visible warning and continue without creating any
        # ApiKey/UsagePlan resources.
        if cfg.security.api_key_required:
            cdk.Annotations.of(self).add_warning_v2(
                "api-key-not-supported",
                "api_key_required is not supported for EC2 backend in v1; "
                "use ip_allowlist for access restriction. api_key_required is ignored.",
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

        resolved_vpc = vpc if vpc is not None else self._resolve_vpc(cfg)

        FrontendConstruct(self, "Frontend", cfg=cfg, zone=zone)
        self._build_api(cfg, api_domain, zone, resolved_vpc)

        CfnOutput(self, "FrontendUrl", value=f"https://{fe_domain}")
        CfnOutput(self, "ApiUrl", value=f"https://{api_domain}")

    # ------------------------------------------------------------------ #
    # API / EC2                                                          #
    # ------------------------------------------------------------------ #

    def _build_api(
        self,
        cfg: DeployConfig,
        api_domain: str,
        zone: route53.IHostedZone,
        resolved_vpc: ec2.IVpc,
    ) -> None:
        assert isinstance(cfg.backend, Ec2Backend)
        ec2_cfg = cfg.backend.ec2

        api_cert = acm.Certificate(
            self,
            "ApiCert",
            domain_name=api_domain,
            validation=acm.CertificateValidation.from_dns(zone),
        )

        # ---- Security groups ----
        alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=resolved_vpc, description="ALB inbound")
        instance_sg = ec2.SecurityGroup(
            self, "InstanceSg", vpc=resolved_vpc, description="Instance inbound"
        )
        # Instance only accepts traffic from the ALB on the app port.
        instance_sg.add_ingress_rule(
            ec2.Peer.security_group_id(alb_sg.security_group_id),
            ec2.Port.tcp(ec2_cfg.app_port),
        )

        if cfg.security.ip_allowlist:
            for cidr in cfg.security.ip_allowlist:
                alb_sg.add_ingress_rule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(443))
                alb_sg.add_ingress_rule(ec2.Peer.ipv4(cidr), ec2.Port.tcp(80))
        else:
            alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443))
            alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80))

        # ---- IAM role ----
        instance_role = iam.Role(
            self,
            "InstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore"),
            ],
        )
        for grant in ec2_cfg.iam_grants:
            instance_role.add_to_policy(
                iam.PolicyStatement(actions=grant.actions, resources=grant.resources)
            )

        # ---- App asset (zipped directory, no Docker required) ----
        asset = s3_assets.Asset(self, "AppAsset", path=ec2_cfg.source_path)
        asset.grant_read(instance_role)

        # ---- User data with CFN Fn::Sub for secrets and asset references ----
        # sub_vars are substituted by CFN at stack create/update time:
        #   - Asset refs resolve to the CDK-uploaded S3 bucket/key.
        #   - {{resolve:secretsmanager:...}} refs resolve to secret values.
        # CDK base64-encodes the result automatically when attaching to the
        # LaunchTemplate — do NOT call Fn::Base64 manually.
        sub_vars: dict[str, Any] = {
            "AssetBucket": asset.s3_bucket_name,
            "AssetKey": asset.s3_object_key,
        }
        env_lines: list[str] = []

        for sec in ec2_cfg.secrets:
            assert sec.aws_secret_name is not None
            var = f"Secret_{sec.name}"
            sub_vars[var] = "{{resolve:secretsmanager:" + sec.aws_secret_name + ":SecretString}}"
            env_lines.append(f"Environment={sec.name}=${{{var}}}")
            sm = secretsmanager.Secret.from_secret_name_v2(
                self, f"Sec-{sec.name}", sec.aws_secret_name
            )
            sm.grant_read(instance_role)

        for k, v in ec2_cfg.env_vars.items():
            env_lines.append(f"Environment={k}={v}")

        user_data = ec2.UserData.custom(
            self._build_user_data(ec2_cfg.app_entrypoint, env_lines, sub_vars)
        )

        # ---- Machine image ----
        machine_image: ec2.IMachineImage = (
            ec2.MachineImage.generic_linux({cfg.aws_region: ec2_cfg.ami_id})
            if ec2_cfg.ami_id
            else ec2.MachineImage.latest_amazon_linux2023()
        )

        # ---- Launch template (all mutable attributes live here; changing any
        #      triggers ASG replacing_update → new instance) ----
        key_pair = (
            ec2.KeyPair.from_key_pair_name(self, "KeyPair", ec2_cfg.key_name)
            if ec2_cfg.key_name
            else None
        )
        launch_template = ec2.LaunchTemplate(
            self,
            "LaunchTemplate",
            instance_type=ec2.InstanceType(ec2_cfg.instance_type),
            machine_image=machine_image,
            user_data=user_data,
            security_group=instance_sg,
            role=instance_role,
            key_pair=key_pair,
        )

        # ---- Subnet selection ----
        public_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)
        if ec2_cfg.private_subnet and ec2_cfg.vpc is not None:
            instance_subnets = ec2.SubnetSelection(
                subnets=[
                    ec2.Subnet.from_subnet_id(self, f"PrivSub{i}", sid)
                    for i, sid in enumerate(ec2_cfg.vpc.private_subnet_ids)
                ]
            )
        else:
            instance_subnets = public_subnets

        # ---- ASG (min=max=1; replacing_update replaces the instance on change) ----
        asg = autoscaling.AutoScalingGroup(
            self,
            "AppAsg",
            vpc=resolved_vpc,
            launch_template=launch_template,
            min_capacity=1,
            max_capacity=1,
            desired_capacity=1,
            vpc_subnets=instance_subnets,
            update_policy=autoscaling.UpdatePolicy.replacing_update(),
        )

        # ---- ALB ----
        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=resolved_vpc,
            internet_facing=True,
            vpc_subnets=public_subnets,
            security_group=alb_sg,
        )

        target_group = elbv2.ApplicationTargetGroup(
            self,
            "AppTargetGroup",
            vpc=resolved_vpc,
            port=ec2_cfg.app_port,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[asg],
            health_check=elbv2.HealthCheck(
                path=ec2_cfg.health_check_path,
                healthy_http_codes="200",
            ),
        )

        # HTTP → HTTPS redirect (CDK convenience method adds a 301 redirect listener)
        alb.add_redirect(
            source_port=80,
            source_protocol=elbv2.ApplicationProtocol.HTTP,
            target_port=443,
            target_protocol=elbv2.ApplicationProtocol.HTTPS,
        )

        alb.add_listener(
            "HttpsListener",
            port=443,
            certificates=[api_cert],
            default_target_groups=[target_group],
        )

        route53.ARecord(
            self,
            "ApiRecord",
            zone=zone,
            record_name=record_name(cfg.project, cfg.stage, prefix="api."),
            target=route53.RecordTarget.from_alias(route53_targets.LoadBalancerTarget(alb)),
        )

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _resolve_vpc(self, cfg: DeployConfig) -> ec2.IVpc:
        assert isinstance(cfg.backend, Ec2Backend)
        ec2_cfg = cfg.backend.ec2
        if ec2_cfg.vpc is not None:
            return ec2.Vpc.from_vpc_attributes(
                self,
                "Vpc",
                vpc_id=ec2_cfg.vpc.vpc_id,
                availability_zones=[cfg.aws_region + "a", cfg.aws_region + "b"],
                public_subnet_ids=ec2_cfg.vpc.public_subnet_ids,
                private_subnet_ids=ec2_cfg.vpc.private_subnet_ids or None,
            )
        return ec2.Vpc.from_lookup(self, "Vpc", is_default=True)

    @staticmethod
    def _build_user_data(
        app_entrypoint: str,
        env_lines: list[str],
        sub_vars: dict[str, Any],
    ) -> str:
        env_block = "\n".join(env_lines)
        # ${AssetBucket} and ${AssetKey} are CFN Fn::Sub variables resolved to the
        # CDK bootstrap asset's S3 coordinates at deploy time. ${Secret_*} variables
        # are resolved from Secrets Manager by CFN before base64-encoding user data.
        template = (
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            "\n"
            "dnf install -y python3.12 unzip\n"
            "\n"
            "aws s3 cp s3://${AssetBucket}/${AssetKey} /tmp/app.zip\n"
            "mkdir -p /opt/app\n"
            "unzip -o /tmp/app.zip -d /opt/app\n"
            "\n"
            "python3.12 -m ensurepip --upgrade\n"
            "python3.12 -m pip install -r /opt/app/requirements.txt\n"
            "\n"
            "cat > /etc/systemd/system/app.service << 'SVCEOF'\n"
            "[Unit]\n"
            "Description=Application Service\n"
            "After=network.target\n"
            "\n"
            "[Service]\n"
            f"ExecStart={app_entrypoint}\n"
            "WorkingDirectory=/opt/app\n"
            "Restart=on-failure\n"
            "StandardOutput=journal\n"
            "StandardError=journal\n"
            f"{env_block}\n"
            "\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
            "SVCEOF\n"
            "\n"
            "systemctl daemon-reload\n"
            "systemctl enable --now app.service\n"
        )
        # CDK wraps this token in Fn::Base64 automatically when attached to the
        # LaunchTemplate — do NOT call cdk.Fn.base64() here.
        return cast(str, cdk.Fn.sub(template, sub_vars))  # type: ignore[redundant-cast,unused-ignore]
