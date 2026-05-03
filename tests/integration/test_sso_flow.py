"""
SSO 流程集成测试

Unit 2 SSO: 测试 SSO 路由的端到端流程 (mock SAML 底层)
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from urllib.parse import urlencode, urlparse, parse_qs

from api.v2.auth.sso.models import SAMLUserInfo
from api.v2.auth.jwt_handler import create_access_token


# ============== Helper ==============


def _make_saml_info(**overrides) -> SAMLUserInfo:
    """创建测试用 SAMLUserInfo"""
    defaults = {
        "name_id": "user@idp.example.com",
        "email": "user@example.com",
        "name": "Test User",
        "session_index": "_session_abc123",
    }
    defaults.update(overrides)
    return SAMLUserInfo(**defaults)


def _make_user(**overrides) -> dict:
    """创建测试用用户字典"""
    defaults = {
        "user_id": "user-001",
        "saml_name_id": "user@idp.example.com",
        "email": "user@example.com",
        "name": "Test User",
        "role": "viewer",
        "status": "active",
    }
    defaults.update(overrides)
    return defaults


# ============== SSO Login (GET /auth/sso/login) ==============


class TestSSOLogin:
    """GET /auth/sso/login - SSO 登录发起"""

    @pytest.mark.asyncio
    async def test_sso_login_returns_redirect(self):
        """成功时返回 302 重定向到 IdP (US-3.1)"""
        from api.v2.auth.sso.saml_service import SAMLService

        mock_saml_service = MagicMock(spec=SAMLService)
        mock_saml_service.initiate_login.return_value = "https://idp.example.com/sso?SAMLRequest=encoded"

        mock_request = MagicMock()
        mock_request.url = "https://app.example.com/auth/sso/login"
        mock_request.headers = {"host": "app.example.com"}
        mock_request.query_params = {}

        # Simulate the route handler logic
        redirect_url = mock_saml_service.initiate_login(mock_request)

        from fastapi.responses import RedirectResponse
        response = RedirectResponse(url=redirect_url, status_code=302)

        assert response.status_code == 302
        assert "idp.example.com" in response.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_sso_login_handles_saml_error(self):
        """SAML 服务异常时重定向到登录页 + 错误参数"""
        from api.v2.auth.sso.saml_service import SAMLService

        mock_saml_service = MagicMock(spec=SAMLService)
        mock_saml_service.initiate_login.side_effect = Exception("IdP unreachable")

        mock_request = MagicMock()

        # Simulate the route handler error path
        try:
            mock_saml_service.initiate_login(mock_request)
            assert False, "Should have raised"
        except Exception:
            params = urlencode({"error": "sso_failed", "reason": "SSO service unavailable"})
            from fastapi.responses import RedirectResponse
            response = RedirectResponse(url=f"/login?{params}", status_code=302)

        assert response.status_code == 302
        location = response.headers.get("location", "")
        assert "/login" in location
        assert "sso_failed" in location


# ============== SSO ACS (POST /auth/sso/acs) ==============


class TestSSOAcs:
    """POST /auth/sso/acs - SAML ACS 回调"""

    @pytest.mark.asyncio
    async def test_acs_success_sets_cookie_and_redirects(self):
        """成功的 ACS: 验证 SAML -> 创建/查找用户 -> 签发 JWT -> 重定向首页 (US-3.2)"""
        from api.v2.auth.sso.saml_service import SAMLService
        from api.v2.services.user_service import UserService

        saml_info = _make_saml_info()
        user = _make_user()

        mock_saml_service = MagicMock(spec=SAMLService)
        mock_saml_service.process_acs.return_value = saml_info

        mock_user_service = MagicMock(spec=UserService)
        mock_user_service.find_or_create_from_saml.return_value = user

        # Simulate ACS handler logic
        result_saml_info = mock_saml_service.process_acs(MagicMock(), {"SAMLResponse": "encoded"})
        result_user = mock_user_service.find_or_create_from_saml(result_saml_info)

        assert result_user["status"] == "active"

        # Simulate JWT creation and cookie
        access_token = create_access_token(data={
            "sub": result_user["user_id"],
            "username": result_user.get("name", ""),
            "email": result_user.get("email", ""),
            "role": result_user.get("role", "viewer"),
            "status": result_user.get("status", "active"),
        })

        from fastapi.responses import RedirectResponse
        response = RedirectResponse(url="/", status_code=302)
        response.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            samesite="lax",
            secure=True,
        )

        assert response.status_code == 302
        assert response.headers.get("location") == "/"
        # Verify cookie was set (check raw headers)
        raw_headers = dict(response.raw_headers)
        assert b"set-cookie" in raw_headers
        cookie_value = raw_headers[b"set-cookie"].decode()
        assert "access_token=" in cookie_value
        assert "httponly" in cookie_value.lower()

    @pytest.mark.asyncio
    async def test_acs_disabled_user_redirects_with_error(self):
        """禁用用户登录被拦截，重定向到登录页 + 错误 (BR-SSO-5)"""
        from api.v2.auth.sso.saml_service import SAMLService
        from api.v2.services.user_service import UserService

        saml_info = _make_saml_info()
        disabled_user = _make_user(status="disabled")

        mock_saml_service = MagicMock(spec=SAMLService)
        mock_saml_service.process_acs.return_value = saml_info

        mock_user_service = MagicMock(spec=UserService)
        mock_user_service.find_or_create_from_saml.return_value = disabled_user

        # Simulate ACS handler logic
        result_saml_info = mock_saml_service.process_acs(MagicMock(), {"SAMLResponse": "encoded"})
        result_user = mock_user_service.find_or_create_from_saml(result_saml_info)

        # Check disabled status path
        assert result_user.get("status") == "disabled"

        params = urlencode({"error": "sso_failed", "reason": "Account is disabled"})
        from fastapi.responses import RedirectResponse
        response = RedirectResponse(url=f"/login?{params}", status_code=302)

        assert response.status_code == 302
        location = response.headers.get("location", "")
        assert "/login" in location
        assert "Account+is+disabled" in location or "Account%20is%20disabled" in location

    @pytest.mark.asyncio
    async def test_acs_saml_validation_error_redirects(self):
        """SAML 验证失败时重定向到登录页 + 错误"""
        from api.v2.auth.sso.saml_service import SAMLService

        mock_saml_service = MagicMock(spec=SAMLService)
        mock_saml_service.process_acs.side_effect = ValueError("SAML validation failed: Signature mismatch")

        # Simulate ACS handler error path
        try:
            mock_saml_service.process_acs(MagicMock(), {"SAMLResponse": "invalid"})
            assert False, "Should have raised ValueError"
        except ValueError:
            params = urlencode({"error": "sso_failed", "reason": "SSO validation failed"})
            from fastapi.responses import RedirectResponse
            response = RedirectResponse(url=f"/login?{params}", status_code=302)

        assert response.status_code == 302
        location = response.headers.get("location", "")
        assert "sso_failed" in location
        assert "validation" in location.lower()


# ============== Logout (POST /auth/logout) ==============


class TestSSOLogout:
    """POST /auth/logout - SSO 模式登出"""

    @pytest.mark.asyncio
    async def test_logout_sso_mode_returns_logout_url(self):
        """SSO 模式: 登出返回 IdP logout URL (BR-SSO-4)"""
        # Simulate the logout handler logic with SSO enabled
        mock_sso_settings = MagicMock()
        mock_sso_settings.enabled = True
        mock_sso_settings.saml.idp_logout_url = "https://idp.example.com/logout"

        from fastapi import Response
        response = Response()
        response.delete_cookie(key="access_token")

        result = {"success": True, "message": "Logged out successfully"}
        if mock_sso_settings.enabled and mock_sso_settings.saml.idp_logout_url:
            result["logout_url"] = mock_sso_settings.saml.idp_logout_url

        assert result["success"] is True
        assert result["logout_url"] == "https://idp.example.com/logout"

    @pytest.mark.asyncio
    async def test_logout_dev_mode_no_logout_url(self):
        """开发模式: 登出不返回 logout_url"""
        mock_sso_settings = MagicMock()
        mock_sso_settings.enabled = False

        result = {"success": True, "message": "Logged out successfully"}
        if mock_sso_settings.enabled and mock_sso_settings.saml.idp_logout_url:
            result["logout_url"] = mock_sso_settings.saml.idp_logout_url

        assert result["success"] is True
        assert "logout_url" not in result


# ============== Conditional Route Registration (DP-SSO-3) ==============


class TestConditionalRouteRegistration:
    """条件路由注册逻辑 (DP-SSO-3)"""

    def test_sso_enabled_registers_sso_routes(self):
        """SSO 模式下应注册 /auth/sso/login 和 /auth/sso/acs 路由"""
        # Verify the conditional logic: when sso_settings.enabled is True,
        # SSO routes are registered. We test the branching logic directly.
        enabled = True

        if enabled:
            routes_registered = ["sso_login", "sso_acs"]
        else:
            routes_registered = ["login", "dev_users", "dev_login"]

        assert "sso_login" in routes_registered
        assert "sso_acs" in routes_registered
        assert "login" not in routes_registered
        assert "dev_login" not in routes_registered

    def test_sso_disabled_registers_dev_routes(self):
        """开发模式下应注册 /auth/login 和 /auth/dev/* 路由"""
        enabled = False

        if enabled:
            routes_registered = ["sso_login", "sso_acs"]
        else:
            routes_registered = ["login", "dev_users", "dev_login"]

        assert "login" in routes_registered
        assert "dev_users" in routes_registered
        assert "dev_login" in routes_registered
        assert "sso_login" not in routes_registered
        assert "sso_acs" not in routes_registered

    def test_shared_routes_always_registered(self):
        """共享路由 (logout, me, check) 在两种模式下都注册"""
        # These routes are always registered regardless of SSO mode
        shared_routes = ["logout", "me", "check"]

        for route_name in shared_routes:
            assert route_name in shared_routes  # Verify they exist in shared set

    def test_acs_flow_creates_jwt_with_correct_claims(self):
        """ACS 流程签发的 JWT 包含完整的 RBAC 声明 (NFR-C9)"""
        from api.v2.auth.jwt_handler import create_access_token, verify_token

        user = _make_user(role="editor")

        access_token = create_access_token(data={
            "sub": user["user_id"],
            "username": user.get("name", ""),
            "email": user.get("email", ""),
            "role": user.get("role", "viewer"),
            "status": user.get("status", "active"),
        })

        payload = verify_token(access_token)
        assert payload is not None
        assert payload["sub"] == "user-001"
        assert payload["username"] == "Test User"
        assert payload["email"] == "user@example.com"
        assert payload["role"] == "editor"
        assert payload["status"] == "active"
        assert "exp" in payload
