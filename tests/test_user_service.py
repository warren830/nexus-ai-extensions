"""
UserService 测试

Step 14: 测试 UserService 方法 + 首个用户自动 admin 逻辑
"""
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture
def mock_db():
    """Mock DynamoDBClient"""
    return MagicMock()


@pytest.fixture
def user_service(mock_db):
    """创建 UserService 实例"""
    from api.v2.services.user_service import UserService
    return UserService(db=mock_db)


SAMPLE_USER = {
    "user_id": "user-001",
    "email": "test@example.com",
    "name": "Test User",
    "role": "viewer",
    "status": "active",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


class TestGetUserById:
    def test_found(self, user_service, mock_db):
        """获取存在的用户"""
        mock_db.get_user_by_id.return_value = SAMPLE_USER.copy()
        result = user_service.get_user_by_id("user-001")

        mock_db.get_user_by_id.assert_called_once_with("user-001")
        assert result["user_id"] == "user-001"

    def test_not_found(self, user_service, mock_db):
        """获取不存在的用户返回 None"""
        mock_db.get_user_by_id.return_value = None
        result = user_service.get_user_by_id("nonexistent")

        assert result is None


class TestGetUserByEmail:
    def test_found(self, user_service, mock_db):
        """按邮箱获取用户"""
        mock_db.get_user_by_email.return_value = SAMPLE_USER.copy()
        result = user_service.get_user_by_email("test@example.com")

        assert result["email"] == "test@example.com"


class TestListUsers:
    def test_list_users(self, user_service, mock_db):
        """列出所有用户"""
        mock_db.list_all_users.return_value = [SAMPLE_USER.copy()]
        result = user_service.list_users()

        mock_db.list_all_users.assert_called_once()
        assert len(result) == 1


class TestCountUsers:
    def test_count_users(self, user_service, mock_db):
        """统计用户数"""
        mock_db.count_users.return_value = 5
        result = user_service.count_users()

        assert result == 5


class TestCreateUser:
    def test_first_user_auto_admin(self, user_service, mock_db):
        """首个用户自动 admin (BR-1.1)"""
        mock_db.count_users.return_value = 0
        mock_db.create_user.side_effect = lambda data: data

        result = user_service._create_user({"name": "First User", "email": "first@example.com"})

        assert result["role"] == "admin"
        assert "user_id" in result
        assert "created_at" in result

    def test_subsequent_user_default_viewer(self, user_service, mock_db):
        """后续用户默认 viewer (BR-1.2)"""
        mock_db.count_users.return_value = 1
        mock_db.create_user.side_effect = lambda data: data

        result = user_service._create_user({"name": "Second User", "email": "second@example.com"})

        assert result["role"] == "viewer"

    def test_explicit_role_preserved(self, user_service, mock_db):
        """显式设置 role 时不覆盖"""
        mock_db.count_users.return_value = 5
        mock_db.create_user.side_effect = lambda data: data

        result = user_service._create_user({
            "name": "Editor",
            "email": "editor@example.com",
            "role": "editor",
        })

        assert result["role"] == "editor"

    def test_auto_uuid(self, user_service, mock_db):
        """自动生成 UUID"""
        mock_db.count_users.return_value = 0
        mock_db.create_user.side_effect = lambda data: data

        result = user_service._create_user({"name": "X"})

        assert "user_id" in result
        assert len(result["user_id"]) == 36  # UUID4 format
