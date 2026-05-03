"""
SAMLSettings / SSOSettings 配置测试

Unit 2 SSO: 测试 SAML 配置加载、验证、以及 SSOSettings 整合逻辑
"""
import pytest
import os
import tempfile
from unittest.mock import patch, MagicMock

from api.v2.auth.config import SAMLSettings


class TestSAMLSettingsInit:
    """SAMLSettings 初始化"""

    def test_valid_config_dict(self):
        """有效配置字典正确初始化所有字段"""
        config = {
            "sp_entity_id": "my-app",
            "sp_acs_url": "https://app.example.com/auth/sso/acs",
            "idp_metadata_file": "/tmp/idp-metadata.xml",
            "idp_logout_url": "https://idp.example.com/logout",
        }
        settings = SAMLSettings(config)

        assert settings.sp_entity_id == "my-app"
        assert settings.sp_acs_url == "https://app.example.com/auth/sso/acs"
        assert settings.idp_metadata_file == "/tmp/idp-metadata.xml"
        assert settings.idp_logout_url == "https://idp.example.com/logout"

    def test_default_values(self):
        """空配置字典使用默认值"""
        settings = SAMLSettings({})

        assert settings.sp_entity_id == "nexus-ai"
        assert settings.sp_acs_url == ""
        assert settings.idp_metadata_file == "config/saml/idp-metadata.xml"
        assert settings.idp_logout_url == ""


class TestSAMLSettingsValidateRequired:
    """SAMLSettings.validate_required() 验证"""

    def test_validate_required_passes_with_all_fields(self):
        """所有必需字段存在且 metadata 文件存在时通过验证"""
        # 创建临时文件模拟 IdP metadata
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
            f.write(b"<EntityDescriptor/>")
            metadata_path = f.name

        try:
            config = {
                "sp_entity_id": "nexus-ai",
                "sp_acs_url": "https://app.example.com/auth/sso/acs",
                "idp_metadata_file": metadata_path,
                "idp_logout_url": "https://idp.example.com/logout",
            }
            settings = SAMLSettings(config)
            # Should not raise
            settings.validate_required()
        finally:
            os.unlink(metadata_path)

    def test_validate_required_raises_when_sp_acs_url_empty(self):
        """sp_acs_url 为空时抛出 RuntimeError"""
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
            f.write(b"<EntityDescriptor/>")
            metadata_path = f.name

        try:
            config = {
                "sp_acs_url": "",
                "idp_metadata_file": metadata_path,
                "idp_logout_url": "https://idp.example.com/logout",
            }
            settings = SAMLSettings(config)

            with pytest.raises(RuntimeError, match="sso.saml.sp_acs_url"):
                settings.validate_required()
        finally:
            os.unlink(metadata_path)

    def test_validate_required_raises_when_idp_metadata_file_not_found(self):
        """idp_metadata_file 不存在时抛出 RuntimeError"""
        config = {
            "sp_acs_url": "https://app.example.com/auth/sso/acs",
            "idp_metadata_file": "/nonexistent/path/idp-metadata.xml",
            "idp_logout_url": "https://idp.example.com/logout",
        }
        settings = SAMLSettings(config)

        with pytest.raises(RuntimeError, match="IdP metadata file"):
            settings.validate_required()

    def test_validate_required_raises_when_idp_logout_url_empty(self):
        """idp_logout_url 为空时抛出 RuntimeError"""
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as f:
            f.write(b"<EntityDescriptor/>")
            metadata_path = f.name

        try:
            config = {
                "sp_acs_url": "https://app.example.com/auth/sso/acs",
                "idp_metadata_file": metadata_path,
                "idp_logout_url": "",
            }
            settings = SAMLSettings(config)

            with pytest.raises(RuntimeError, match="sso.saml.idp_logout_url"):
                settings.validate_required()
        finally:
            os.unlink(metadata_path)

    def test_validate_required_reports_all_missing_fields(self):
        """多个字段缺失时错误信息包含所有缺失项"""
        config = {
            "sp_acs_url": "",
            "idp_metadata_file": "/nonexistent/path/metadata.xml",
            "idp_logout_url": "",
        }
        settings = SAMLSettings(config)

        with pytest.raises(RuntimeError) as exc_info:
            settings.validate_required()

        error_msg = str(exc_info.value)
        assert "sp_acs_url" in error_msg
        assert "idp_logout_url" in error_msg
        assert "IdP metadata file" in error_msg


class TestSSOSettingsFromYAML:
    """SSOSettings 从 YAML 加载 SAML 配置"""

    @patch("api.v2.auth.config._load_sso_from_config")
    def test_sso_settings_loads_saml_from_yaml(self, mock_load):
        """SSOSettings 从 YAML 配置加载 SAML 子配置"""
        mock_load.return_value = {
            "enabled": False,
            "jwt_secret_key": "test-secret-key-for-jwt",
            "saml": {
                "sp_entity_id": "yaml-nexus",
                "sp_acs_url": "https://yaml.example.com/acs",
                "idp_metadata_file": "config/saml/idp.xml",
                "idp_logout_url": "https://yaml.example.com/logout",
            },
        }

        # 清除环境变量以确保从 YAML 加载
        env_vars_to_clear = [
            "SSO_ENABLED", "SSO_JWT_SECRET_KEY",
            "SSO_SAML_SP_ENTITY_ID", "SSO_SAML_SP_ACS_URL",
            "SSO_SAML_IDP_METADATA_FILE", "SSO_SAML_IDP_LOGOUT_URL",
        ]
        with patch.dict(os.environ, {}, clear=False):
            for var in env_vars_to_clear:
                os.environ.pop(var, None)

            from api.v2.auth.config import SSOSettings
            settings = SSOSettings()

        assert settings.saml.sp_entity_id == "yaml-nexus"
        assert settings.saml.sp_acs_url == "https://yaml.example.com/acs"
        assert settings.jwt_secret_key == "test-secret-key-for-jwt"

    @patch("api.v2.auth.config._load_sso_from_config")
    def test_sso_settings_disabled_does_not_validate(self, mock_load):
        """enabled=false 时不调用 validate_required"""
        mock_load.return_value = {
            "enabled": False,
            "saml": {
                "sp_acs_url": "",  # 无效值，但不应触发验证
                "idp_metadata_file": "/nonexistent",
                "idp_logout_url": "",
            },
        }

        env_vars_to_clear = [
            "SSO_ENABLED", "SSO_JWT_SECRET_KEY",
            "SSO_SAML_SP_ENTITY_ID", "SSO_SAML_SP_ACS_URL",
            "SSO_SAML_IDP_METADATA_FILE", "SSO_SAML_IDP_LOGOUT_URL",
        ]
        with patch.dict(os.environ, {}, clear=False):
            for var in env_vars_to_clear:
                os.environ.pop(var, None)

            from api.v2.auth.config import SSOSettings
            # Should NOT raise, because enabled=False skips validation
            settings = SSOSettings()

        assert settings.enabled is False
