"""
SAMLService 测试

Unit 2 SSO: 测试 SAML 登录发起、ACS 回调验证、属性提取
"""
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from api.v2.auth.sso.models import SAMLUserInfo
from api.v2.auth.sso.saml_service import _extract_attribute


# ============== _extract_attribute (module-level function) ==============


class TestExtractAttribute:
    """SAML 属性提取"""

    def test_standard_key(self):
        """标准键名提取"""
        attributes = {
            "email": ["user@example.com"],
            "name": ["John Doe"],
        }
        assert _extract_attribute(attributes, ["email"]) == "user@example.com"

    def test_uri_format_key(self):
        """URI 格式键名提取 (SAML 2.0 标准)"""
        attributes = {
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress": ["user@example.com"],
        }
        keys = [
            "email",
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        ]
        assert _extract_attribute(attributes, keys) == "user@example.com"

    def test_fallback_priority(self):
        """按候选键名顺序优先匹配"""
        attributes = {
            "displayName": ["Display Name"],
            "name": ["Short Name"],
        }
        # "name" listed first, should be returned
        keys = ["name", "displayName"]
        assert _extract_attribute(attributes, keys) == "Short Name"

    def test_empty_attributes(self):
        """属性字典为空返回空字符串"""
        assert _extract_attribute({}, ["email", "name"]) == ""

    def test_key_exists_but_empty_list(self):
        """键存在但值列表为空，跳过到下一个候选键"""
        attributes = {
            "email": [],
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress": ["fallback@example.com"],
        }
        keys = [
            "email",
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        ]
        assert _extract_attribute(attributes, keys) == "fallback@example.com"

    def test_no_matching_key(self):
        """无匹配键返回空字符串"""
        attributes = {"other": ["value"]}
        assert _extract_attribute(attributes, ["email", "name"]) == ""


# ============== SAMLService ==============


class TestSAMLServiceInitiateLogin:
    """SAMLService.initiate_login()"""

    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_Auth")
    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_IdPMetadataParser")
    def test_initiate_login_returns_redirect_url(self, mock_parser, mock_auth_cls):
        """initiate_login 调用 auth.login() 并返回 IdP 重定向 URL"""
        # Mock IdP metadata parser
        mock_parser.parse.return_value = {
            "idp": {
                "entityId": "https://idp.example.com",
                "singleSignOnService": {"url": "https://idp.example.com/sso"},
            }
        }

        # Mock OneLogin_Saml2_Auth instance
        mock_auth_instance = MagicMock()
        mock_auth_instance.login.return_value = "https://idp.example.com/sso?SAMLRequest=encoded"
        mock_auth_cls.return_value = mock_auth_instance

        from api.v2.auth.config import SAMLSettings
        from api.v2.auth.sso.saml_service import SAMLService

        saml_settings = SAMLSettings({
            "sp_entity_id": "nexus-ai",
            "sp_acs_url": "https://app.example.com/auth/sso/acs",
            "idp_metadata_file": "fake-metadata.xml",
            "idp_logout_url": "https://idp.example.com/logout",
        })

        # Mock open() in _build_saml_settings
        with patch("builtins.open", MagicMock(return_value=MagicMock(read=MagicMock(return_value="<xml/>")))):
            service = SAMLService(saml_settings)

        # Create mock request
        mock_request = MagicMock()
        mock_request.url = "https://app.example.com/auth/sso/login"
        mock_request.headers = {"host": "app.example.com"}
        mock_request.query_params = {}

        result = service.initiate_login(mock_request)

        assert result == "https://idp.example.com/sso?SAMLRequest=encoded"
        mock_auth_instance.login.assert_called_once()


class TestSAMLServiceProcessAcs:
    """SAMLService.process_acs()"""

    def _create_service_with_mocks(self, mock_parser, mock_auth_cls):
        """Helper: 创建带 mock 的 SAMLService 实例"""
        mock_parser.parse.return_value = {
            "idp": {
                "entityId": "https://idp.example.com",
                "singleSignOnService": {"url": "https://idp.example.com/sso"},
            }
        }

        from api.v2.auth.config import SAMLSettings
        from api.v2.auth.sso.saml_service import SAMLService

        saml_settings = SAMLSettings({
            "sp_entity_id": "nexus-ai",
            "sp_acs_url": "https://app.example.com/auth/sso/acs",
            "idp_metadata_file": "fake-metadata.xml",
            "idp_logout_url": "https://idp.example.com/logout",
        })

        with patch("builtins.open", MagicMock(return_value=MagicMock(read=MagicMock(return_value="<xml/>")))):
            service = SAMLService(saml_settings)

        return service

    def _create_mock_request(self):
        """Helper: 创建 mock FastAPI Request"""
        mock_request = MagicMock()
        mock_request.url = "https://app.example.com/auth/sso/acs"
        mock_request.headers = {"host": "app.example.com"}
        mock_request.query_params = {}
        return mock_request

    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_Auth")
    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_IdPMetadataParser")
    def test_process_acs_success(self, mock_parser, mock_auth_cls):
        """成功的 ACS 回调提取 SAMLUserInfo"""
        service = self._create_service_with_mocks(mock_parser, mock_auth_cls)

        # Configure mock auth for ACS
        mock_auth_instance = MagicMock()
        mock_auth_instance.get_errors.return_value = []
        mock_auth_instance.get_nameid.return_value = "user@idp.example.com"
        mock_auth_instance.get_attributes.return_value = {
            "email": ["user@example.com"],
            "name": ["John Doe"],
        }
        mock_auth_instance.get_session_index.return_value = "_session_abc123"
        mock_auth_cls.return_value = mock_auth_instance

        mock_request = self._create_mock_request()
        post_data = {"SAMLResponse": "base64-encoded-response"}

        result = service.process_acs(mock_request, post_data)

        assert isinstance(result, SAMLUserInfo)
        assert result.name_id == "user@idp.example.com"
        assert result.email == "user@example.com"
        assert result.name == "John Doe"
        assert result.session_index == "_session_abc123"
        mock_auth_instance.process_response.assert_called_once()

    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_Auth")
    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_IdPMetadataParser")
    def test_process_acs_raises_on_saml_errors(self, mock_parser, mock_auth_cls):
        """SAML 验证失败时抛出 ValueError"""
        service = self._create_service_with_mocks(mock_parser, mock_auth_cls)

        mock_auth_instance = MagicMock()
        mock_auth_instance.get_errors.return_value = ["invalid_response"]
        mock_auth_instance.get_last_error_reason.return_value = "Signature validation failed"
        mock_auth_cls.return_value = mock_auth_instance

        mock_request = self._create_mock_request()
        post_data = {"SAMLResponse": "invalid-response"}

        with pytest.raises(ValueError, match="SAML validation failed"):
            service.process_acs(mock_request, post_data)

    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_Auth")
    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_IdPMetadataParser")
    def test_process_acs_raises_when_nameid_missing(self, mock_parser, mock_auth_cls):
        """NameID 缺失时抛出 ValueError"""
        service = self._create_service_with_mocks(mock_parser, mock_auth_cls)

        mock_auth_instance = MagicMock()
        mock_auth_instance.get_errors.return_value = []
        mock_auth_instance.get_nameid.return_value = None  # Missing NameID
        mock_auth_instance.get_attributes.return_value = {}
        mock_auth_cls.return_value = mock_auth_instance

        mock_request = self._create_mock_request()
        post_data = {"SAMLResponse": "response-without-nameid"}

        with pytest.raises(ValueError, match="SAML Response missing NameID"):
            service.process_acs(mock_request, post_data)

    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_Auth")
    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_IdPMetadataParser")
    def test_process_acs_name_fallback_to_nameid(self, mock_parser, mock_auth_cls):
        """name 属性缺失时回退到 NameID"""
        service = self._create_service_with_mocks(mock_parser, mock_auth_cls)

        mock_auth_instance = MagicMock()
        mock_auth_instance.get_errors.return_value = []
        mock_auth_instance.get_nameid.return_value = "user@idp.example.com"
        mock_auth_instance.get_attributes.return_value = {
            "email": ["user@example.com"],
            # no "name" attribute
        }
        mock_auth_instance.get_session_index.return_value = None
        mock_auth_cls.return_value = mock_auth_instance

        mock_request = self._create_mock_request()
        result = service.process_acs(mock_request, {"SAMLResponse": "encoded"})

        # name should fallback to name_id
        assert result.name == "user@idp.example.com"
        assert result.session_index is None


class TestPrepareRequestData:
    """SAMLService.prepare_request_data()"""

    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_Auth")
    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_IdPMetadataParser")
    def test_prepare_request_data_https(self, mock_parser, mock_auth_cls):
        """HTTPS 请求构造正确的 request dict"""
        mock_parser.parse.return_value = {"idp": {}}

        from api.v2.auth.config import SAMLSettings
        from api.v2.auth.sso.saml_service import SAMLService

        saml_settings = SAMLSettings({
            "sp_acs_url": "https://app.example.com/auth/sso/acs",
            "idp_metadata_file": "fake.xml",
        })

        with patch("builtins.open", MagicMock(return_value=MagicMock(read=MagicMock(return_value="<xml/>")))):
            service = SAMLService(saml_settings)

        mock_request = MagicMock()
        mock_request.url = "https://app.example.com/auth/sso/login?next=/"
        mock_request.headers = {"host": "app.example.com"}
        mock_request.query_params = {"next": "/"}

        result = service.prepare_request_data(mock_request)

        assert result["https"] == "on"
        assert result["http_host"] == "app.example.com"
        assert result["script_name"] == "/auth/sso/login"
        assert result["get_data"] == {"next": "/"}
        assert result["post_data"] == {}

    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_Auth")
    @patch("api.v2.auth.sso.saml_service.OneLogin_Saml2_IdPMetadataParser")
    def test_prepare_request_data_http(self, mock_parser, mock_auth_cls):
        """HTTP 请求 https 标记为 off"""
        mock_parser.parse.return_value = {"idp": {}}

        from api.v2.auth.config import SAMLSettings
        from api.v2.auth.sso.saml_service import SAMLService

        saml_settings = SAMLSettings({
            "sp_acs_url": "http://localhost:8000/auth/sso/acs",
            "idp_metadata_file": "fake.xml",
        })

        with patch("builtins.open", MagicMock(return_value=MagicMock(read=MagicMock(return_value="<xml/>")))):
            service = SAMLService(saml_settings)

        mock_request = MagicMock()
        mock_request.url = "http://localhost:8000/auth/sso/login"
        mock_request.headers = {"host": "localhost:8000"}
        mock_request.query_params = {}

        result = service.prepare_request_data(mock_request)

        assert result["https"] == "off"
        assert result["http_host"] == "localhost:8000"
