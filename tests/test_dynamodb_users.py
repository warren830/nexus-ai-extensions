"""
DynamoDB Users CRUD 测试

Step 4: 数据访问层测试 — 测试 DynamoDBClient 中 Users 相关方法
"""
import pytest
from unittest.mock import MagicMock, patch
from decimal import Decimal


@pytest.fixture
def mock_dynamo_resource():
    """Mock DynamoDB boto3 resource"""
    with patch('api.v2.database.dynamodb.boto3') as mock_boto3:
        mock_resource = MagicMock()
        mock_boto3.resource.return_value = mock_resource
        yield mock_resource


@pytest.fixture
def mock_users_table():
    """Mock users DynamoDB table"""
    return MagicMock()


@pytest.fixture
def db_client(mock_dynamo_resource, mock_users_table):
    """Create a DynamoDBClient with mocked tables"""
    mock_dynamo_resource.Table.return_value = mock_users_table

    with patch('api.v2.database.dynamodb.DynamoDBClient._instance', None):
        from api.v2.database.dynamodb import DynamoDBClient
        client = DynamoDBClient()
        # Inject mock table
        client._users_table = mock_users_table
        yield client


SAMPLE_USER = {
    "user_id": "test-user-001",
    "saml_name_id": "test@idp.example.com",
    "email": "test@example.com",
    "name": "Test User",
    "role": "viewer",
    "status": "active",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


class TestCreateUser:
    def test_create_user_success(self, db_client, mock_users_table):
        """创建用户: put_item 调用且返回用户数据"""
        mock_users_table.put_item.return_value = {}
        result = db_client.create_user(SAMPLE_USER.copy())

        mock_users_table.put_item.assert_called_once()
        assert result["user_id"] == "test-user-001"
        assert result["role"] == "viewer"

    def test_create_user_sets_timestamps(self, db_client, mock_users_table):
        """创建用户: 自动设置 created_at/updated_at"""
        mock_users_table.put_item.return_value = {}
        user_data = {"user_id": "u1", "name": "X"}
        result = db_client.create_user(user_data)

        assert "created_at" in result
        assert "updated_at" in result


class TestGetUserById:
    def test_get_user_found(self, db_client, mock_users_table):
        """按 ID 获取: 存在的用户"""
        mock_users_table.get_item.return_value = {"Item": SAMPLE_USER.copy()}
        result = db_client.get_user_by_id("test-user-001")

        mock_users_table.get_item.assert_called_once_with(Key={"user_id": "test-user-001"})
        assert result is not None
        assert result["user_id"] == "test-user-001"

    def test_get_user_not_found(self, db_client, mock_users_table):
        """按 ID 获取: 不存在的用户返回 None"""
        mock_users_table.get_item.return_value = {}
        result = db_client.get_user_by_id("nonexistent")

        assert result is None


class TestGetUserBySamlNameId:
    def test_get_user_by_saml_found(self, db_client, mock_users_table):
        """按 SAML NameID 获取: GSI 查询"""
        mock_users_table.query.return_value = {"Items": [SAMPLE_USER.copy()]}
        result = db_client.get_user_by_saml_name_id("test@idp.example.com")

        assert result is not None
        assert result["saml_name_id"] == "test@idp.example.com"

    def test_get_user_by_saml_not_found(self, db_client, mock_users_table):
        """按 SAML NameID 获取: 不存在返回 None"""
        mock_users_table.query.return_value = {"Items": []}
        result = db_client.get_user_by_saml_name_id("unknown@idp.example.com")

        assert result is None


class TestGetUserByEmail:
    def test_get_user_by_email_found(self, db_client, mock_users_table):
        """按 Email 获取: GSI 查询"""
        mock_users_table.query.return_value = {"Items": [SAMPLE_USER.copy()]}
        result = db_client.get_user_by_email("test@example.com")

        assert result is not None
        assert result["email"] == "test@example.com"

    def test_get_user_by_email_not_found(self, db_client, mock_users_table):
        """按 Email 获取: 不存在返回 None"""
        mock_users_table.query.return_value = {"Items": []}
        result = db_client.get_user_by_email("nobody@example.com")

        assert result is None


class TestUpdateUser:
    def test_update_user(self, db_client, mock_users_table):
        """更新用户: update_item 调用"""
        updated = SAMPLE_USER.copy()
        updated["role"] = "editor"
        mock_users_table.update_item.return_value = {"Attributes": updated}

        result = db_client.update_user("test-user-001", {"role": "editor"})

        mock_users_table.update_item.assert_called_once()
        call_kwargs = mock_users_table.update_item.call_args[1]
        assert call_kwargs["Key"] == {"user_id": "test-user-001"}
        assert "ReturnValues" in call_kwargs


class TestListAllUsers:
    def test_list_users(self, db_client, mock_users_table):
        """列出所有用户: scan 调用"""
        mock_users_table.scan.return_value = {"Items": [SAMPLE_USER.copy()]}
        result = db_client.list_all_users()

        mock_users_table.scan.assert_called_once()
        assert len(result) == 1
        assert result[0]["user_id"] == "test-user-001"

    def test_list_users_empty(self, db_client, mock_users_table):
        """列出所有用户: 空表"""
        mock_users_table.scan.return_value = {"Items": []}
        result = db_client.list_all_users()

        assert result == []


class TestCountUsers:
    def test_count_users(self, db_client, mock_users_table):
        """统计用户数"""
        mock_users_table.scan.return_value = {"Count": 3}
        result = db_client.count_users()

        mock_users_table.scan.assert_called_once_with(Select='COUNT')
        assert result == 3

    def test_count_users_empty(self, db_client, mock_users_table):
        """统计用户数: 空表返回 0"""
        mock_users_table.scan.return_value = {"Count": 0}
        result = db_client.count_users()

        assert result == 0
