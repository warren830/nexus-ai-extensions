"""
认证中间件测试

Step 10: 测试 get_current_user() 返回值扩展 + 新旧 JWT 兼容
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi import HTTPException


@pytest.fixture
def mock_request():
    """Mock FastAPI Request"""
    request = MagicMock()
    request.cookies = {}
    return request


class TestGetCurrentUser:
    @pytest.mark.asyncio
    async def test_full_payload(self, mock_request):
        """完整 payload: 返回所有字段"""
        from api.v2.auth.middleware import get_current_user

        full_payload = {
            "sub": "user-001",
            "username": "testuser",
            "email": "test@example.com",
            "role": "editor",
            "status": "active",
            "exp": 9999999999,
        }

        mock_creds = MagicMock()
        mock_creds.credentials = "valid-token"

        with patch('api.v2.auth.middleware.verify_token', return_value=full_payload):
            result = await get_current_user(mock_request, mock_creds)

        assert result["user_id"] == "user-001"
        assert result["username"] == "testuser"
        assert result["email"] == "test@example.com"
        assert result["role"] == "editor"
        assert result["status"] == "active"

    @pytest.mark.asyncio
    async def test_legacy_payload_defaults(self, mock_request):
        """旧 JWT payload: 缺少 role/email 使用默认值"""
        from api.v2.auth.middleware import get_current_user

        legacy_payload = {
            "sub": "admin",
            "exp": 9999999999,
        }

        mock_creds = MagicMock()
        mock_creds.credentials = "legacy-token"

        with patch('api.v2.auth.middleware.verify_token', return_value=legacy_payload):
            result = await get_current_user(mock_request, mock_creds)

        assert result["user_id"] == "admin"
        assert result["username"] == "admin"  # fallback to sub
        assert result["email"] == ""
        assert result["role"] == "viewer"  # default
        assert result["status"] == "active"  # default

    @pytest.mark.asyncio
    async def test_no_token_raises_401(self, mock_request):
        """无 token: 抛出 401"""
        from api.v2.auth.middleware import get_current_user

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(mock_request, None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_raises_401(self, mock_request):
        """无效 token: 抛出 401"""
        from api.v2.auth.middleware import get_current_user

        mock_creds = MagicMock()
        mock_creds.credentials = "invalid-token"

        with patch('api.v2.auth.middleware.verify_token', return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(mock_request, mock_creds)
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_cookie_token_fallback(self, mock_request):
        """Cookie token 回退: header 无 token 时使用 cookie"""
        from api.v2.auth.middleware import get_current_user

        mock_request.cookies = {"access_token": "cookie-token"}
        payload = {"sub": "cookie-user", "role": "viewer", "exp": 9999999999}

        with patch('api.v2.auth.middleware.verify_token', return_value=payload):
            result = await get_current_user(mock_request, None)

        assert result["user_id"] == "cookie-user"


class TestGetOptionalUser:
    @pytest.mark.asyncio
    async def test_returns_user_when_valid(self, mock_request):
        """可选认证: 有效 token 返回用户"""
        from api.v2.auth.middleware import get_optional_user

        payload = {"sub": "u1", "role": "admin", "exp": 9999999999}
        mock_creds = MagicMock()
        mock_creds.credentials = "valid"

        with patch('api.v2.auth.middleware.verify_token', return_value=payload):
            result = await get_optional_user(mock_request, mock_creds)

        assert result is not None
        assert result["user_id"] == "u1"

    @pytest.mark.asyncio
    async def test_returns_none_when_invalid(self, mock_request):
        """可选认证: 无 token 返回 None"""
        from api.v2.auth.middleware import get_optional_user

        result = await get_optional_user(mock_request, None)
        assert result is None
