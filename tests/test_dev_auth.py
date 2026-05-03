"""
DevAuth 测试

Step 14: 测试开发模式认证 — 3 种角色登录 + 模式检查 + 无效 user_id
"""
import pytest
from unittest.mock import patch


class TestIsDevMode:
    def test_dev_mode_when_sso_disabled(self):
        """SSO 未启用时为开发模式"""
        from api.v2.auth.dev_auth import is_dev_mode

        with patch('api.v2.auth.dev_auth.sso_settings') as mock_settings:
            mock_settings.enabled = False
            assert is_dev_mode() is True

    def test_not_dev_mode_when_sso_enabled(self):
        """SSO 启用时非开发模式"""
        from api.v2.auth.dev_auth import is_dev_mode

        with patch('api.v2.auth.dev_auth.sso_settings') as mock_settings:
            mock_settings.enabled = True
            assert is_dev_mode() is False


class TestGetDevUsers:
    def test_returns_3_users(self):
        """返回 3 个预设用户"""
        from api.v2.auth.dev_auth import get_dev_users

        users = get_dev_users()
        assert len(users) == 3

    def test_user_fields(self):
        """用户包含必要字段 (不含 status/saml_name_id)"""
        from api.v2.auth.dev_auth import get_dev_users

        users = get_dev_users()
        for user in users:
            assert "user_id" in user
            assert "name" in user
            assert "email" in user
            assert "role" in user
            assert "status" not in user  # 敏感信息不暴露
            assert "saml_name_id" not in user

    def test_all_roles_present(self):
        """包含 admin, editor, viewer 三种角色"""
        from api.v2.auth.dev_auth import get_dev_users

        roles = {u["role"] for u in get_dev_users()}
        assert roles == {"admin", "editor", "viewer"}


class TestGetDevUser:
    def test_get_existing_user(self):
        """获取存在的开发用户"""
        from api.v2.auth.dev_auth import get_dev_user

        user = get_dev_user("dev-admin-001")
        assert user is not None
        assert user["role"] == "admin"

    def test_get_nonexistent_user(self):
        """获取不存在的 user_id 返回 None"""
        from api.v2.auth.dev_auth import get_dev_user

        user = get_dev_user("nonexistent")
        assert user is None


class TestDevLogin:
    def test_login_admin(self):
        """开发模式登录: admin"""
        from api.v2.auth.dev_auth import dev_login

        token, user = dev_login("dev-admin-001")
        assert isinstance(token, str)
        assert len(token) > 0
        assert user["role"] == "admin"
        assert user["name"] == "dev-admin"

    def test_login_editor(self):
        """开发模式登录: editor"""
        from api.v2.auth.dev_auth import dev_login

        token, user = dev_login("dev-editor-001")
        assert user["role"] == "editor"

    def test_login_viewer(self):
        """开发模式登录: viewer"""
        from api.v2.auth.dev_auth import dev_login

        token, user = dev_login("dev-viewer-001")
        assert user["role"] == "viewer"

    def test_login_invalid_user_raises(self):
        """无效 user_id 抛出 ValueError"""
        from api.v2.auth.dev_auth import dev_login

        with pytest.raises(ValueError, match="Invalid dev user"):
            dev_login("invalid-user-id")

    def test_login_returns_valid_jwt(self):
        """登录返回可验证的 JWT"""
        from api.v2.auth.dev_auth import dev_login
        from api.v2.auth.jwt_handler import verify_token

        token, user = dev_login("dev-editor-001")
        payload = verify_token(token)

        assert payload is not None
        assert payload["sub"] == "dev-editor-001"
        assert payload["role"] == "editor"
        assert payload["username"] == "dev-editor"
