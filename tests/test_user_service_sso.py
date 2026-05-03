"""
UserService SSO 相关测试

Unit 2 SSO: 测试 find_or_create_from_saml 查找/创建用户逻辑
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from api.v2.services.user_service import UserService
from api.v2.auth.sso.models import SAMLUserInfo


@pytest.fixture
def mock_db():
    """Mock DynamoDBClient"""
    return MagicMock()


@pytest.fixture
def user_service(mock_db):
    """创建 UserService 实例"""
    return UserService(db=mock_db)


SAMPLE_SAML_INFO = SAMLUserInfo(
    name_id="user@idp.example.com",
    email="user@example.com",
    name="Test User",
    session_index="_session_abc123",
)


class TestFindOrCreateFromSaml:
    """UserService.find_or_create_from_saml()"""

    def test_find_existing_user_updates_last_login(self, user_service, mock_db):
        """找到已有用户时更新 last_login_at (BR-SSO-2.3)"""
        existing_user = {
            "user_id": "user-001",
            "saml_name_id": "user@idp.example.com",
            "email": "user@example.com",
            "name": "Test User",
            "role": "editor",
            "status": "active",
            "created_at": "2026-01-01T00:00:00Z",
        }
        mock_db.get_user_by_saml_name_id.return_value = existing_user.copy()

        result = user_service.find_or_create_from_saml(SAMPLE_SAML_INFO)

        # Should look up by SAML NameID
        mock_db.get_user_by_saml_name_id.assert_called_once_with("user@idp.example.com")

        # Should update last_login_at
        mock_db.update_user.assert_called_once()
        call_args = mock_db.update_user.call_args
        assert call_args[0][0] == "user-001"
        assert "last_login_at" in call_args[0][1]

        # Should return user with updated last_login_at
        assert result["user_id"] == "user-001"
        assert "last_login_at" in result
        assert result["role"] == "editor"

    def test_find_existing_user_does_not_update_name_or_email(self, user_service, mock_db):
        """再次登录不更新 name 和 email (BR-SSO-2.3)"""
        existing_user = {
            "user_id": "user-001",
            "saml_name_id": "user@idp.example.com",
            "email": "old@example.com",
            "name": "Old Name",
            "role": "viewer",
            "status": "active",
        }
        mock_db.get_user_by_saml_name_id.return_value = existing_user.copy()

        # SAML info has different email/name
        saml_info = SAMLUserInfo(
            name_id="user@idp.example.com",
            email="new@example.com",
            name="New Name",
        )

        result = user_service.find_or_create_from_saml(saml_info)

        # update_user should only receive last_login_at, not name/email
        update_data = mock_db.update_user.call_args[0][1]
        assert "email" not in update_data
        assert "name" not in update_data
        assert "last_login_at" in update_data

    def test_create_new_user_when_not_found(self, user_service, mock_db):
        """未找到用户时创建新用户 (BR-SSO-3)"""
        mock_db.get_user_by_saml_name_id.return_value = None
        mock_db.count_users.return_value = 5  # Not the first user
        mock_db.create_user.side_effect = lambda data: data

        result = user_service.find_or_create_from_saml(SAMPLE_SAML_INFO)

        # Should attempt lookup first
        mock_db.get_user_by_saml_name_id.assert_called_once_with("user@idp.example.com")

        # Should create user
        mock_db.create_user.assert_called_once()
        created_data = mock_db.create_user.call_args[0][0]
        assert created_data["saml_name_id"] == "user@idp.example.com"
        assert created_data["email"] == "user@example.com"
        assert created_data["name"] == "Test User"

        # Should have auto-generated fields
        assert "user_id" in created_data
        assert "created_at" in created_data
        assert "status" in created_data
        assert created_data["status"] == "active"

    def test_first_user_gets_admin_role(self, user_service, mock_db):
        """首个用户自动获得 admin 角色 (BR-1.1)"""
        mock_db.get_user_by_saml_name_id.return_value = None
        mock_db.count_users.return_value = 0  # First user
        mock_db.create_user.side_effect = lambda data: data

        result = user_service.find_or_create_from_saml(SAMPLE_SAML_INFO)

        assert result["role"] == "admin"

    def test_subsequent_user_gets_viewer_role(self, user_service, mock_db):
        """后续用户获得 viewer 角色 (BR-1.2)"""
        mock_db.get_user_by_saml_name_id.return_value = None
        mock_db.count_users.return_value = 3  # Not the first user
        mock_db.create_user.side_effect = lambda data: data

        result = user_service.find_or_create_from_saml(SAMPLE_SAML_INFO)

        assert result["role"] == "viewer"

    def test_created_user_has_saml_name_id(self, user_service, mock_db):
        """新建用户包含 saml_name_id 字段"""
        mock_db.get_user_by_saml_name_id.return_value = None
        mock_db.count_users.return_value = 1
        mock_db.create_user.side_effect = lambda data: data

        result = user_service.find_or_create_from_saml(SAMPLE_SAML_INFO)

        assert result["saml_name_id"] == "user@idp.example.com"

    def test_created_user_uuid_format(self, user_service, mock_db):
        """新建用户自动生成 UUID4 格式的 user_id"""
        mock_db.get_user_by_saml_name_id.return_value = None
        mock_db.count_users.return_value = 0
        mock_db.create_user.side_effect = lambda data: data

        result = user_service.find_or_create_from_saml(SAMPLE_SAML_INFO)

        assert len(result["user_id"]) == 36  # UUID4 format: 8-4-4-4-12
