"""
RBAC 模型与权限校验测试

Step 10: 测试 has_permission() 各种角色组合 + require_role() 工厂函数
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from api.v2.auth.rbac.models import RoleLevel, ROLE_HIERARCHY, has_permission


class TestRoleLevel:
    def test_role_values(self):
        """角色枚举值"""
        assert RoleLevel.VIEWER == "viewer"
        assert RoleLevel.EDITOR == "editor"
        assert RoleLevel.ADMIN == "admin"

    def test_role_hierarchy_order(self):
        """角色层级顺序: viewer < editor < admin"""
        assert ROLE_HIERARCHY[RoleLevel.VIEWER] < ROLE_HIERARCHY[RoleLevel.EDITOR]
        assert ROLE_HIERARCHY[RoleLevel.EDITOR] < ROLE_HIERARCHY[RoleLevel.ADMIN]


class TestHasPermission:
    def test_admin_has_all_permissions(self):
        """admin 拥有所有权限"""
        assert has_permission("admin", "viewer") is True
        assert has_permission("admin", "editor") is True
        assert has_permission("admin", "admin") is True

    def test_editor_has_viewer_and_editor(self):
        """editor 拥有 viewer 和 editor 权限"""
        assert has_permission("editor", "viewer") is True
        assert has_permission("editor", "editor") is True
        assert has_permission("editor", "admin") is False

    def test_viewer_only_viewer(self):
        """viewer 仅拥有 viewer 权限"""
        assert has_permission("viewer", "viewer") is True
        assert has_permission("viewer", "editor") is False
        assert has_permission("viewer", "admin") is False

    def test_unknown_role_denied(self):
        """未知角色被拒绝"""
        assert has_permission("unknown", "viewer") is False
        assert has_permission("", "viewer") is False

    def test_unknown_required_role_denied(self):
        """未知的要求角色 (level=999) 总是被拒绝"""
        assert has_permission("admin", "superadmin") is False


class TestRequireRole:
    @pytest.mark.asyncio
    async def test_require_role_passes(self):
        """require_role: 角色满足时返回用户"""
        from api.v2.auth.rbac.permissions import require_role

        checker = require_role("viewer")
        mock_user = {"user_id": "u1", "role": "admin", "status": "active"}

        with patch('api.v2.auth.rbac.permissions.get_current_user', return_value=mock_user):
            result = await checker(current_user=mock_user)
            assert result == mock_user

    @pytest.mark.asyncio
    async def test_require_role_denies(self):
        """require_role: 角色不足时 403"""
        from api.v2.auth.rbac.permissions import require_role
        from fastapi import HTTPException

        checker = require_role("admin")
        mock_user = {"user_id": "u1", "role": "viewer", "status": "active"}

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=mock_user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_require_role_default_viewer(self):
        """require_role: 无 role 字段默认 viewer"""
        from api.v2.auth.rbac.permissions import require_role

        checker = require_role("viewer")
        mock_user = {"user_id": "u1", "status": "active"}

        result = await checker(current_user=mock_user)
        assert result == mock_user


class TestRequireActiveUser:
    @pytest.mark.asyncio
    async def test_active_user_passes(self):
        """require_active_user: 活跃用户通过"""
        from api.v2.auth.rbac.permissions import require_active_user

        checker = require_active_user()
        mock_user = {"user_id": "u1", "role": "viewer", "status": "active"}

        result = await checker(current_user=mock_user)
        assert result == mock_user

    @pytest.mark.asyncio
    async def test_disabled_user_denied(self):
        """require_active_user: 禁用用户 403"""
        from api.v2.auth.rbac.permissions import require_active_user
        from fastapi import HTTPException

        checker = require_active_user()
        mock_user = {"user_id": "u1", "role": "admin", "status": "disabled"}

        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=mock_user)
        assert exc_info.value.status_code == 403
        assert "disabled" in exc_info.value.detail.lower()
