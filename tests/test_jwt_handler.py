"""
JWT Handler 测试

Step 10: 测试扩展 payload 编解码 + SECRET_KEY 配置化
"""
import pytest
from datetime import timedelta
from unittest.mock import patch


class TestCreateAccessToken:
    def test_create_token_with_full_payload(self):
        """创建 token: 完整 payload"""
        from api.v2.auth.jwt_handler import create_access_token, verify_token

        data = {
            "sub": "user-001",
            "username": "testuser",
            "email": "test@example.com",
            "role": "editor",
            "status": "active",
        }

        token = create_access_token(data)
        assert isinstance(token, str)
        assert len(token) > 0

        # 验证解码
        payload = verify_token(token)
        assert payload is not None
        assert payload["sub"] == "user-001"
        assert payload["username"] == "testuser"
        assert payload["email"] == "test@example.com"
        assert payload["role"] == "editor"
        assert payload["status"] == "active"
        assert "exp" in payload

    def test_create_token_with_minimal_payload(self):
        """创建 token: 最小 payload (仅 sub)"""
        from api.v2.auth.jwt_handler import create_access_token, verify_token

        data = {"sub": "admin"}
        token = create_access_token(data)
        payload = verify_token(token)

        assert payload is not None
        assert payload["sub"] == "admin"

    def test_custom_expiry(self):
        """创建 token: 自定义过期时间"""
        from api.v2.auth.jwt_handler import create_access_token, verify_token

        data = {"sub": "u1"}
        token = create_access_token(data, expires_delta=timedelta(hours=1))
        payload = verify_token(token)

        assert payload is not None
        assert payload["sub"] == "u1"


class TestVerifyToken:
    def test_verify_valid_token(self):
        """验证有效 token"""
        from api.v2.auth.jwt_handler import create_access_token, verify_token

        token = create_access_token({"sub": "u1", "role": "admin"})
        payload = verify_token(token)

        assert payload is not None
        assert payload["sub"] == "u1"
        assert payload["role"] == "admin"

    def test_verify_invalid_token(self):
        """验证无效 token 返回 None"""
        from api.v2.auth.jwt_handler import verify_token

        result = verify_token("invalid.token.here")
        assert result is None

    def test_verify_tampered_token(self):
        """验证被篡改的 token 返回 None"""
        from api.v2.auth.jwt_handler import create_access_token, verify_token

        token = create_access_token({"sub": "u1"})
        # 篡改 token
        tampered = token[:-5] + "XXXXX"
        result = verify_token(tampered)

        assert result is None

    def test_verify_expired_token(self):
        """验证过期 token 返回 None"""
        from api.v2.auth.jwt_handler import create_access_token, verify_token

        token = create_access_token({"sub": "u1"}, expires_delta=timedelta(seconds=-1))
        result = verify_token(token)

        assert result is None
