"""Unit tests for scripts/ and infra/secrets_bootstrap.py.

All boto3 calls are mocked — no real AWS connections made.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from infra.config_loader import load_config
from infra.config_schema import DeployConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
LAMBDA_MINIMAL = REPO_ROOT / "examples" / "lambda-minimal"
EC2_MINIMAL = REPO_ROOT / "examples" / "ec2-minimal"


# --------------------------------------------------------------------------- #
# Shared fixtures                                                             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def lambda_cfg() -> DeployConfig:
    return load_config(LAMBDA_MINIMAL / "deploy.yaml", "prod", validate_paths=False)


@pytest.fixture
def ec2_cfg() -> DeployConfig:
    """EC2-minimal config (prod) — already has DB_PASSWORD declared."""
    return load_config(EC2_MINIMAL / "deploy.yaml", "prod", validate_paths=False)


# --------------------------------------------------------------------------- #
# deploy.py                                                                   #
# --------------------------------------------------------------------------- #


class TestDeployValidateStage:
    def test_rejects_staging(self) -> None:
        from scripts.deploy import validate_stage

        with pytest.raises(SystemExit, match="staging"):
            validate_stage("staging")

    def test_accepts_dev(self) -> None:
        from scripts.deploy import validate_stage

        validate_stage("dev")  # must not raise

    def test_accepts_prod(self) -> None:
        from scripts.deploy import validate_stage

        validate_stage("prod")  # must not raise

    def test_rejects_empty(self) -> None:
        from scripts.deploy import validate_stage

        with pytest.raises(SystemExit):
            validate_stage("")


class TestDeployCheckAccount:
    def test_matching_account_passes(self, lambda_cfg: DeployConfig) -> None:
        from scripts.deploy import check_account

        with patch("scripts.deploy.boto3.client") as mock_boto3:
            sts: MagicMock = MagicMock()
            sts.get_caller_identity.return_value = {"Account": lambda_cfg.aws_account}
            mock_boto3.return_value = sts
            check_account(lambda_cfg)  # should not raise

    def test_mismatched_account_aborts(self, lambda_cfg: DeployConfig) -> None:
        from scripts.deploy import check_account

        with patch("scripts.deploy.boto3.client") as mock_boto3:
            sts: MagicMock = MagicMock()
            sts.get_caller_identity.return_value = {"Account": "999999999999"}
            mock_boto3.return_value = sts
            with pytest.raises(SystemExit, match="mismatch"):
                check_account(lambda_cfg)


class TestDeployConfigJs:
    def test_basic_shape(self) -> None:
        from scripts.deploy import generate_config_js

        result = generate_config_js(api_url="https://api.example.com", stage="prod")
        assert "window.APP_CONFIG" in result
        assert '"apiUrl": "https://api.example.com"' in result
        assert '"stage": "prod"' in result
        assert '"apiKey"' not in result

    def test_includes_api_key_when_present(self) -> None:
        from scripts.deploy import generate_config_js

        result = generate_config_js(
            api_url="https://api.example.com", stage="prod", api_key="abc123"
        )
        assert '"apiKey": "abc123"' in result

    def test_no_api_key_when_none(self) -> None:
        from scripts.deploy import generate_config_js

        result = generate_config_js(api_url="https://api.example.com", stage="prod", api_key=None)
        assert "apiKey" not in result


# --------------------------------------------------------------------------- #
# infra/secrets_bootstrap.py                                                  #
# --------------------------------------------------------------------------- #


def _make_sm_client(*, describe_response: Any = None, not_found: bool = False) -> Any:
    """Build a mocked secretsmanager client."""

    class _NotFoundError(Exception):
        pass

    client: MagicMock = MagicMock()
    client.exceptions.ResourceNotFoundException = _NotFoundError

    if not_found:
        client.describe_secret.side_effect = _NotFoundError("Not found")
    elif describe_response is not None:
        client.describe_secret.return_value = describe_response
    else:
        client.describe_secret.return_value = {}  # exists, no DeletedDate

    # Paginator for orphan check: return empty by default
    paginator: MagicMock = MagicMock()
    paginator.paginate.return_value = [{"SecretList": []}]
    client.get_paginator.return_value = paginator

    return client


class TestBootstrapSecrets:
    def test_creates_missing_secret(self, ec2_cfg: DeployConfig) -> None:
        from infra.secrets_bootstrap import bootstrap_secrets

        client = _make_sm_client(not_found=True)
        with (
            patch("infra.secrets_bootstrap.boto3.client", return_value=client),
            patch("infra.secrets_bootstrap.getpass.getpass", return_value="secret123"),
        ):
            bootstrap_secrets(ec2_cfg)

        client.create_secret.assert_called_once()
        call_kwargs = client.create_secret.call_args.kwargs
        assert call_kwargs["Name"] == "ec2-minimal/prod/db_password"
        assert call_kwargs["SecretString"] == "secret123"
        assert {"Key": "Project", "Value": "ec2-minimal"} in call_kwargs["Tags"]

    def test_leaves_existing_secret_untouched(self, ec2_cfg: DeployConfig) -> None:
        from infra.secrets_bootstrap import bootstrap_secrets

        client = _make_sm_client()  # describe returns {} → exists, no deletion
        with patch("infra.secrets_bootstrap.boto3.client", return_value=client):
            bootstrap_secrets(ec2_cfg)

        client.create_secret.assert_not_called()
        client.put_secret_value.assert_not_called()

    def test_aborts_on_deletion_scheduled(self, ec2_cfg: DeployConfig) -> None:
        from infra.secrets_bootstrap import bootstrap_secrets

        client = _make_sm_client(describe_response={"DeletedDate": "2024-01-01"})
        with (
            patch("infra.secrets_bootstrap.boto3.client", return_value=client),
            pytest.raises(SystemExit, match="scheduled for deletion"),
        ):
            bootstrap_secrets(ec2_cfg)

    def test_aborts_on_empty_input(self, ec2_cfg: DeployConfig) -> None:
        from infra.secrets_bootstrap import bootstrap_secrets

        client = _make_sm_client(not_found=True)
        with (
            patch("infra.secrets_bootstrap.boto3.client", return_value=client),
            patch("infra.secrets_bootstrap.getpass.getpass", return_value=""),
            pytest.raises(SystemExit, match="Empty input"),
        ):
            bootstrap_secrets(ec2_cfg)

        client.create_secret.assert_not_called()

    def test_warns_orphans_without_deleting(
        self, ec2_cfg: DeployConfig, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from infra.secrets_bootstrap import bootstrap_secrets

        client = _make_sm_client()
        # Make the paginator return an orphan secret tagged with the project.
        orphan = {
            "Name": "ec2-minimal/prod/old_key",
            "Tags": [{"Key": "Project", "Value": "ec2-minimal"}],
        }
        client.get_paginator.return_value.paginate.return_value = [{"SecretList": [orphan]}]

        with patch("infra.secrets_bootstrap.boto3.client", return_value=client):
            bootstrap_secrets(ec2_cfg)

        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "ec2-minimal/prod/old_key" in out
        # Orphan must NOT be deleted
        client.delete_secret.assert_not_called()

    def test_no_secrets_declared_is_noop(self, lambda_cfg: DeployConfig) -> None:
        """Lambda-minimal has no secrets — bootstrap should be a no-op."""
        from infra.secrets_bootstrap import bootstrap_secrets

        with patch("infra.secrets_bootstrap.boto3.client") as mock_boto3:
            bootstrap_secrets(lambda_cfg)
        mock_boto3.assert_not_called()


class TestRotateSecret:
    def test_rotate_calls_put_secret_value(self, ec2_cfg: DeployConfig) -> None:
        from infra.secrets_bootstrap import rotate_secret

        client: MagicMock = MagicMock()
        with (
            patch("infra.secrets_bootstrap.boto3.client", return_value=client),
            patch("infra.secrets_bootstrap.getpass.getpass", return_value="newval"),
        ):
            rotate_secret(ec2_cfg, "prod", "DB_PASSWORD")

        client.put_secret_value.assert_called_once_with(
            SecretId="ec2-minimal/prod/db_password", SecretString="newval"
        )

    def test_rotate_empty_input_aborts(self, ec2_cfg: DeployConfig) -> None:
        from infra.secrets_bootstrap import rotate_secret

        client: MagicMock = MagicMock()
        with (
            patch("infra.secrets_bootstrap.boto3.client", return_value=client),
            patch("infra.secrets_bootstrap.getpass.getpass", return_value=""),
            pytest.raises(SystemExit, match="Empty input"),
        ):
            rotate_secret(ec2_cfg, "prod", "DB_PASSWORD")

        client.put_secret_value.assert_not_called()

    def test_rotate_unknown_name_aborts(self, ec2_cfg: DeployConfig) -> None:
        from infra.secrets_bootstrap import rotate_secret

        with pytest.raises(SystemExit, match="UNKNOWN_VAR"):
            rotate_secret(ec2_cfg, "prod", "UNKNOWN_VAR")


class TestDeleteSecret:
    def test_delete_calls_delete_secret_with_7day_window(self, ec2_cfg: DeployConfig) -> None:
        from infra.secrets_bootstrap import delete_secret

        client: MagicMock = MagicMock()
        with patch("infra.secrets_bootstrap.boto3.client", return_value=client):
            delete_secret(ec2_cfg, "prod", "DB_PASSWORD")

        client.delete_secret.assert_called_once_with(
            SecretId="ec2-minimal/prod/db_password", RecoveryWindowInDays=7
        )

    def test_delete_unknown_name_aborts(self, ec2_cfg: DeployConfig) -> None:
        from infra.secrets_bootstrap import delete_secret

        with pytest.raises(SystemExit, match="UNKNOWN_VAR"):
            delete_secret(ec2_cfg, "prod", "UNKNOWN_VAR")


# --------------------------------------------------------------------------- #
# destroy.py                                                                  #
# --------------------------------------------------------------------------- #


class TestDestroyConfirm:
    def test_y_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.destroy import confirm_destroy

        monkeypatch.setattr("builtins.input", lambda _: "y")
        assert confirm_destroy("my-stack-dev") is True

    def test_n_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.destroy import confirm_destroy

        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert confirm_destroy("my-stack-dev") is False

    def test_empty_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.destroy import confirm_destroy

        monkeypatch.setattr("builtins.input", lambda _: "")
        assert confirm_destroy("my-stack-dev") is False

    def test_uppercase_y_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from scripts.destroy import confirm_destroy

        monkeypatch.setattr("builtins.input", lambda _: "Y")
        assert confirm_destroy("my-stack-dev") is True


# --------------------------------------------------------------------------- #
# dev_server.py — file operations only                                        #
# --------------------------------------------------------------------------- #


class TestDevServerCopyConfig:
    def test_copies_config_local_to_config(self, tmp_path: Path) -> None:
        from scripts.dev_server import copy_local_config

        frontend = tmp_path / "frontend"
        frontend.mkdir()
        src = frontend / "config.local.js"
        src.write_text("window.APP_CONFIG = { apiUrl: 'http://localhost:8000', stage: 'local' };")

        copy_local_config(frontend)

        dst = frontend / "config.js"
        assert dst.exists()
        assert dst.read_text() == src.read_text()

    def test_raises_if_config_local_missing(self, tmp_path: Path) -> None:
        from scripts.dev_server import copy_local_config

        frontend = tmp_path / "frontend"
        frontend.mkdir()

        with pytest.raises(SystemExit, match="config.local.js"):
            copy_local_config(frontend)
