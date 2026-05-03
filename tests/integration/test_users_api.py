"""
Users API 集成测试

Unit 3 管理层: 测试 /users 路由端点 (mock UserService 和 auth 依赖)
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from api.v2.auth.jwt_handler import create_access_token


# ============== Helper ==============


def _make_token(role: str = "viewer", user_id: str = "admin-001", status: str = "active") -> str:
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


def _make_user(**overrides) -> dict:
    """创建测试用用户字典"""
    defaults = {
        "user_id": "user-001",
        "email": "user@example.com",
        "name": "Test User",
        "role": "viewer",
        "status": "active",
        "created_at": "2026-01-15T00:00:00Z",
        "last_login_at": "2026-02-01T00:00:00Z",
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def admin_token():
    return _make_token("admin", "admin-001")


@pytest.fixture
def viewer_token():
    return _make_token("viewer", "viewer-001")


@pytest.fixture
def client():
    """创建 TestClient，mock _user_service 模块级变量"""
    from api.v2.main import app
    return TestClient(app)


# ============== GET /api/v2/users (list_users) ==============


class TestListUsers:
    """GET /api/v2/users — 用户列表"""

    def test_list_users_admin(self, client, admin_token):
        """Admin 可以获取用户列表，按 created_at 降序 (US-4.1)"""
        users = [
            _make_user(user_id="u1", created_at="2026-01-01T00:00:00Z"),
            _make_user(user_id="u2", created_at="2026-02-01T00:00:00Z"),
            _make_user(user_id="u3", created_at="2026-01-15T00:00:00Z"),
        ]

        with patch("api.v2.routers.users._user_service") as mock_svc:
            mock_svc.list_users.return_value = users

            resp = client.get("/api/v2/users", headers=_auth_header(admin_token))

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        # 验证按 created_at 降序
        assert data[0]["user_id"] == "u2"
        assert data[1]["user_id"] == "u3"
        assert data[2]["user_id"] == "u1"

    def test_list_users_non_admin_forbidden(self, client, viewer_token):
        """非 Admin 访问用户列表返回 403"""
        resp = client.get("/api/v2/users", headers=_auth_header(viewer_token))

        assert resp.status_code == 403


# ============== GET /api/v2/users/me (get_current_user_profile) ==============


class TestGetCurrentUserProfile:
    """GET /api/v2/users/me — 当前用户信息"""

    def test_get_current_user_profile(self, client, admin_token):
        """返回 DB 中的完整用户信息 (US-4.4)"""
        db_user = _make_user(
            user_id="admin-001",
            email="admin@example.com",
            name="Admin User",
            role="admin",
        )

        with patch("api.v2.routers.users._user_service") as mock_svc:
            mock_svc.get_user_by_id.return_value = db_user

            resp = client.get("/api/v2/users/me", headers=_auth_header(admin_token))

        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "admin-001"
        assert data["email"] == "admin@example.com"
        assert data["name"] == "Admin User"
        assert data["role"] == "admin"

    def test_get_current_user_profile_fallback(self, client, admin_token):
        """DB 查找失败时降级返回 JWT 中的信息"""
        with patch("api.v2.routers.users._user_service") as mock_svc:
            mock_svc.get_user_by_id.return_value = None

            resp = client.get("/api/v2/users/me", headers=_auth_header(admin_token))

        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "admin-001"
        assert data["role"] == "admin"


# ============== PUT /api/v2/users/{user_id}/role ==============


class TestUpdateRole:
    """PUT /api/v2/users/{user_id}/role — 修改角色"""

    def test_update_role_success(self, client, admin_token):
        """Admin 可以修改其他用户角色 (US-4.2)"""
        updated_user = _make_user(user_id="user-target", role="editor")

        with patch("api.v2.routers.users._user_service") as mock_svc:
            mock_svc.update_role.return_value = updated_user

            resp = client.put(
                "/api/v2/users/user-target/role",
                json={"role": "editor"},
                headers=_auth_header(admin_token),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "editor"
        mock_svc.update_role.assert_called_once_with("user-target", "editor", "admin-001")

    def test_update_role_self_rejected(self, client, admin_token):
        """Admin 不能修改自己角色 → 400 (BR-ADM-1.1)"""
        with patch("api.v2.routers.users._user_service") as mock_svc:
            mock_svc.update_role.side_effect = PermissionError("Cannot modify own role")

            resp = client.put(
                "/api/v2/users/admin-001/role",
                json={"role": "viewer"},
                headers=_auth_header(admin_token),
            )

        assert resp.status_code == 400
        assert "Cannot modify own role" in resp.json()["detail"]

    def test_update_role_last_admin_rejected(self, client, admin_token):
        """不能降级最后 Admin → 400 (BR-ADM-2.3)"""
        with patch("api.v2.routers.users._user_service") as mock_svc:
            mock_svc.update_role.side_effect = PermissionError("Cannot remove the last admin")

            resp = client.put(
                "/api/v2/users/user-target/role",
                json={"role": "viewer"},
                headers=_auth_header(admin_token),
            )

        assert resp.status_code == 400
        assert "last admin" in resp.json()["detail"]

    def test_update_role_invalid_rejected(self, client, admin_token):
        """无效角色值 → 400 (BR-ADM-2.1)"""
        with patch("api.v2.routers.users._user_service") as mock_svc:
            mock_svc.update_role.side_effect = ValueError("Invalid role: superadmin")

            resp = client.put(
                "/api/v2/users/user-target/role",
                json={"role": "superadmin"},
                headers=_auth_header(admin_token),
            )

        assert resp.status_code == 400
        assert "Invalid role" in resp.json()["detail"]


# ============== PUT /api/v2/users/{user_id}/status ==============


class TestUpdateStatus:
    """PUT /api/v2/users/{user_id}/status — 修改状态"""

    def test_update_status_success(self, client, admin_token):
        """Admin 可以禁用/启用用户 (US-4.3)"""
        updated_user = _make_user(user_id="user-target", status="disabled")

        with patch("api.v2.routers.users._user_service") as mock_svc:
            mock_svc.update_status.return_value = updated_user

            resp = client.put(
                "/api/v2/users/user-target/status",
                json={"status": "disabled"},
                headers=_auth_header(admin_token),
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "disabled"
        mock_svc.update_status.assert_called_once_with("user-target", "disabled", "admin-001")

    def test_update_status_self_rejected(self, client, admin_token):
        """Admin 不能修改自己状态 → 400 (BR-ADM-1.2)"""
        with patch("api.v2.routers.users._user_service") as mock_svc:
            mock_svc.update_status.side_effect = PermissionError("Cannot modify own status")

            resp = client.put(
                "/api/v2/users/admin-001/status",
                json={"status": "disabled"},
                headers=_auth_header(admin_token),
            )

        assert resp.status_code == 400
        assert "Cannot modify own status" in resp.json()["detail"]

    def test_update_status_user_not_found(self, client, admin_token):
        """用户不存在 → 404"""
        with patch("api.v2.routers.users._user_service") as mock_svc:
            mock_svc.update_status.side_effect = LookupError("User not found")

            resp = client.put(
                "/api/v2/users/nonexistent/status",
                json={"status": "disabled"},
                headers=_auth_header(admin_token),
            )

        assert resp.status_code == 404
        assert "User not found" in resp.json()["detail"]
