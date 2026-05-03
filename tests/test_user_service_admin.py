"""
UserService 管理层测试

Unit 3 管理层: 测试 update_role, update_status, count_active_admins 逻辑
"""
import pytest
from unittest.mock import MagicMock

from api.v2.services.user_service import UserService


@pytest.fixture
def mock_db():
    """Mock DynamoDBClient"""
    return MagicMock()


@pytest.fixture
def user_service(mock_db):
    """创建 UserService 实例"""
    return UserService(db=mock_db)


def _make_user(**overrides) -> dict:
    """创建测试用用户字典"""
    defaults = {
        "user_id": "user-target",
        "email": "target@example.com",
        "name": "Target User",
        "role": "viewer",
        "status": "active",
        "created_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return defaults


ADMIN_USER_ID = "user-admin"
TARGET_USER_ID = "user-target"


# ============== update_role ==============


class TestUpdateRole:
    """UserService.update_role()"""

    def test_update_role_success(self, user_service, mock_db):
        """正常角色变更: viewer -> editor (US-4.2)"""
        mock_db.get_user_by_id.return_value = _make_user(role="viewer")

        result = user_service.update_role(TARGET_USER_ID, "editor", ADMIN_USER_ID)

        mock_db.update_user.assert_called_once_with(TARGET_USER_ID, {"role": "editor"})
        assert result["role"] == "editor"

    def test_update_role_self_operation_rejected(self, user_service, mock_db):
        """禁止修改自己的角色 (BR-ADM-1.1)"""
        with pytest.raises(PermissionError, match="Cannot modify own role"):
            user_service.update_role(ADMIN_USER_ID, "viewer", ADMIN_USER_ID)

        # DB 不应被调用
        mock_db.get_user_by_id.assert_not_called()
        mock_db.update_user.assert_not_called()

    def test_update_role_last_admin_protected(self, user_service, mock_db):
        """最后一个 Admin 不能被降级 (BR-ADM-2.3)"""
        mock_db.get_user_by_id.return_value = _make_user(
            user_id=TARGET_USER_ID, role="admin", status="active",
        )
        # list_all_users 只返回一个 active admin
        mock_db.list_all_users.return_value = [
            _make_user(user_id=TARGET_USER_ID, role="admin", status="active"),
        ]

        with pytest.raises(PermissionError, match="last admin"):
            user_service.update_role(TARGET_USER_ID, "editor", ADMIN_USER_ID)

        mock_db.update_user.assert_not_called()

    def test_update_role_invalid_role(self, user_service, mock_db):
        """无效角色值抛出 ValueError (BR-ADM-2.1)"""
        with pytest.raises(ValueError, match="Invalid role"):
            user_service.update_role(TARGET_USER_ID, "superadmin", ADMIN_USER_ID)

        mock_db.get_user_by_id.assert_not_called()
        mock_db.update_user.assert_not_called()

    def test_update_role_same_role_skipped(self, user_service, mock_db):
        """相同角色跳过更新 (BR-ADM-2.5)"""
        mock_db.get_user_by_id.return_value = _make_user(role="editor")

        result = user_service.update_role(TARGET_USER_ID, "editor", ADMIN_USER_ID)

        # 不应调用 update_user
        mock_db.update_user.assert_not_called()
        assert result["role"] == "editor"

    def test_update_role_user_not_found(self, user_service, mock_db):
        """用户不存在抛出 LookupError"""
        mock_db.get_user_by_id.return_value = None

        with pytest.raises(LookupError, match="User not found"):
            user_service.update_role(TARGET_USER_ID, "editor", ADMIN_USER_ID)

        mock_db.update_user.assert_not_called()


# ============== update_status ==============


class TestUpdateStatus:
    """UserService.update_status()"""

    def test_update_status_success(self, user_service, mock_db):
        """正常状态变更: active -> disabled (US-4.3)"""
        mock_db.get_user_by_id.return_value = _make_user(role="viewer", status="active")

        result = user_service.update_status(TARGET_USER_ID, "disabled", ADMIN_USER_ID)

        mock_db.update_user.assert_called_once_with(TARGET_USER_ID, {"status": "disabled"})
        assert result["status"] == "disabled"

    def test_update_status_self_operation_rejected(self, user_service, mock_db):
        """禁止修改自己的状态 (BR-ADM-1.2)"""
        with pytest.raises(PermissionError, match="Cannot modify own status"):
            user_service.update_status(ADMIN_USER_ID, "disabled", ADMIN_USER_ID)

        mock_db.get_user_by_id.assert_not_called()
        mock_db.update_user.assert_not_called()

    def test_update_status_last_admin_disable_protected(self, user_service, mock_db):
        """最后一个 Admin 不能被禁用 (BR-ADM-3.5)"""
        mock_db.get_user_by_id.return_value = _make_user(
            user_id=TARGET_USER_ID, role="admin", status="active",
        )
        # list_all_users 只返回一个 active admin
        mock_db.list_all_users.return_value = [
            _make_user(user_id=TARGET_USER_ID, role="admin", status="active"),
        ]

        with pytest.raises(PermissionError, match="last admin"):
            user_service.update_status(TARGET_USER_ID, "disabled", ADMIN_USER_ID)

        mock_db.update_user.assert_not_called()

    def test_update_status_invalid_status(self, user_service, mock_db):
        """无效状态值抛出 ValueError (BR-ADM-3.1)"""
        with pytest.raises(ValueError, match="Invalid status"):
            user_service.update_status(TARGET_USER_ID, "banned", ADMIN_USER_ID)

        mock_db.get_user_by_id.assert_not_called()
        mock_db.update_user.assert_not_called()

    def test_update_status_same_status_skipped(self, user_service, mock_db):
        """相同状态跳过更新 (BR-ADM-3.6)"""
        mock_db.get_user_by_id.return_value = _make_user(status="active")

        result = user_service.update_status(TARGET_USER_ID, "active", ADMIN_USER_ID)

        mock_db.update_user.assert_not_called()
        assert result["status"] == "active"


# ============== count_active_admins ==============


class TestCountActiveAdmins:
    """UserService.count_active_admins()"""

    def test_count_active_admins(self, user_service, mock_db):
        """正确统计 active admin 数量"""
        mock_db.list_all_users.return_value = [
            _make_user(user_id="u1", role="admin", status="active"),
            _make_user(user_id="u2", role="admin", status="disabled"),
            _make_user(user_id="u3", role="editor", status="active"),
            _make_user(user_id="u4", role="admin", status="active"),
            _make_user(user_id="u5", role="viewer", status="active"),
        ]

        count = user_service.count_active_admins()

        assert count == 2
        mock_db.list_all_users.assert_called_once()
