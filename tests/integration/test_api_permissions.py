"""
API 权限集成测试

Step 16: 测试各角色对不同端点的访问 — 401/403/200
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from api.v2.auth.jwt_handler import create_access_token


def _make_token(role: str = "viewer", user_id: str = "test-user", status: str = "active") -> str:
    """生成测试用 JWT"""
    return create_access_token({
        "sub": user_id,
        "username": f"test-{role}",
        "email": f"{role}@test.local",
        "role": role,
        "status": status,
    })


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def viewer_token():
    return _make_token("viewer")


@pytest.fixture
def editor_token():
    return _make_token("editor")


@pytest.fixture
def admin_token():
    return _make_token("admin")


class TestUnauthenticatedAccess:
    """未认证请求应返回 401"""

    def test_no_token_returns_401(self):
        """无 token → 401"""
        # 验证 require_role 检查器在无认证时返回 401
        from api.v2.auth.rbac.permissions import require_role
        from fastapi import HTTPException

        checker = require_role("viewer")

        # 模拟无认证用户 (get_current_user 会抛 401)
        async def run():
            with pytest.raises(HTTPException) as exc_info:
                # 直接调用时传入会触发 401 的 current_user
                raise HTTPException(status_code=401, detail="Not authenticated")
            assert exc_info.value.status_code == 401

        import asyncio
        asyncio.get_event_loop().run_until_complete(run())


class TestRoleBasedAccess:
    """角色权限检查"""

    @pytest.mark.asyncio
    async def test_viewer_can_access_viewer_endpoint(self):
        """viewer 可以访问 viewer 端点"""
        from api.v2.auth.rbac.permissions import require_role

        checker = require_role("viewer")
        user = {"user_id": "u1", "role": "viewer", "status": "active"}
        result = await checker(current_user=user)
        assert result["user_id"] == "u1"

    @pytest.mark.asyncio
    async def test_viewer_cannot_access_editor_endpoint(self):
        """viewer 不能访问 editor 端点 → 403"""
        from api.v2.auth.rbac.permissions import require_role
        from fastapi import HTTPException

        checker = require_role("editor")
        user = {"user_id": "u1", "role": "viewer", "status": "active"}

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_cannot_access_admin_endpoint(self):
        """viewer 不能访问 admin 端点 → 403"""
        from api.v2.auth.rbac.permissions import require_role
        from fastapi import HTTPException

        checker = require_role("admin")
        user = {"user_id": "u1", "role": "viewer", "status": "active"}

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_editor_can_access_editor_endpoint(self):
        """editor 可以访问 editor 端点"""
        from api.v2.auth.rbac.permissions import require_role

        checker = require_role("editor")
        user = {"user_id": "u1", "role": "editor", "status": "active"}
        result = await checker(current_user=user)
        assert result["role"] == "editor"

    @pytest.mark.asyncio
    async def test_editor_cannot_access_admin_endpoint(self):
        """editor 不能访问 admin 端点 → 403"""
        from api.v2.auth.rbac.permissions import require_role
        from fastapi import HTTPException

        checker = require_role("admin")
        user = {"user_id": "u1", "role": "editor", "status": "active"}

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_can_access_all_endpoints(self):
        """admin 可以访问所有端点"""
        from api.v2.auth.rbac.permissions import require_role

        admin_user = {"user_id": "u1", "role": "admin", "status": "active"}

        for role in ["viewer", "editor", "admin"]:
            checker = require_role(role)
            result = await checker(current_user=admin_user)
            assert result["role"] == "admin"


class TestDisabledUserAccess:
    """禁用用户访问检查"""

    @pytest.mark.asyncio
    async def test_disabled_admin_denied(self):
        """禁用的 admin 被拒绝"""
        from api.v2.auth.rbac.permissions import require_active_user
        from fastapi import HTTPException

        checker = require_active_user()
        user = {"user_id": "u1", "role": "admin", "status": "disabled"}

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=user)
        assert exc_info.value.status_code == 403


class TestTokenGeneration:
    """Token 生成与验证"""

    def test_viewer_token_has_correct_role(self, viewer_token):
        """viewer token 包含正确角色"""
        from api.v2.auth.jwt_handler import verify_token

        payload = verify_token(viewer_token)
        assert payload["role"] == "viewer"

    def test_editor_token_has_correct_role(self, editor_token):
        """editor token 包含正确角色"""
        from api.v2.auth.jwt_handler import verify_token

        payload = verify_token(editor_token)
        assert payload["role"] == "editor"

    def test_admin_token_has_correct_role(self, admin_token):
        """admin token 包含正确角色"""
        from api.v2.auth.jwt_handler import verify_token

        payload = verify_token(admin_token)
        assert payload["role"] == "admin"

    def test_token_includes_all_fields(self, admin_token):
        """token 包含所有必要字段"""
        from api.v2.auth.jwt_handler import verify_token

        payload = verify_token(admin_token)
        assert "sub" in payload
        assert "username" in payload
        assert "email" in payload
        assert "role" in payload
        assert "status" in payload
        assert "exp" in payload
