"""
U0.4 - Integration tests for browser_agent config + nexus_browser_sessions DDB table.

Validates:
- `browser_agent` config block is defined in config/default_config.yaml with
  expected defaults (enabled, token_ttl_seconds, session_ttl_seconds, etc.)
- `nexus_browser_sessions` DDB table schema is registered in the nexus-cli init
  pipeline, with:
  - PK = session_id (HASH)
  - TTL attribute = expires_at
  - GSI UserIdIndex on (user_id HASH, last_used_at RANGE)
- `create_table` is idempotent: existing table returns ('exists', False) — no
  failure on re-run of `./nexus-cli init`.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add project root to path so we can import nexus_utils
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestBrowserAgentConfigLoaded(unittest.TestCase):
    """`browser_agent` config block must exist in default_config.yaml with
    expected defaults (per chrome-extension-mvp design §9)."""

    def test_browser_agent_config_loaded(self):
        from nexus_utils.config_loader import get_config
        config = get_config()
        browser_agent = config.get_section("browser_agent")

        self.assertIsNotNone(
            browser_agent,
            "browser_agent section must be defined in config/default_config.yaml",
        )
        self.assertIsInstance(browser_agent, dict)

        # Required keys with design-mandated defaults
        self.assertTrue(browser_agent.get("enabled"), "enabled must default to true")
        self.assertEqual(browser_agent.get("token_ttl_seconds"), 28800)
        self.assertEqual(browser_agent.get("session_ttl_seconds"), 86400)
        self.assertEqual(browser_agent.get("max_sessions_per_user"), 5)
        self.assertEqual(browser_agent.get("ax_tree_max_nodes"), 2000)
        self.assertEqual(browser_agent.get("request_timeout_seconds"), 30)
        self.assertEqual(browser_agent.get("protocol_version"), "1.0")
        # wss_url: empty string by default, runtime resolves per-request (bridge pattern)
        self.assertIn("wss_url", browser_agent)
        self.assertEqual(browser_agent.get("wss_url"), "")


class TestNexusBrowserSessionsSchemaDefined(unittest.TestCase):
    """`nexus_browser_sessions` table must be included in the infrastructure
    manager's table definitions with correct schema + GSI + TTL."""

    def _find_browser_sessions_table(self):
        """Helper: locate the browser_sessions TableDefinition."""
        from nexus_utils.cli.managers.infrastructure_manager import (
            InfrastructureManager,
        )
        infra = InfrastructureManager()
        table_defs = infra.get_table_definitions()
        matches = [
            t for t in table_defs
            if t.table_name.endswith("browser_sessions")
        ]
        return matches[0] if matches else None, infra

    def test_browser_sessions_table_registered(self):
        table, _ = self._find_browser_sessions_table()
        self.assertIsNotNone(
            table,
            "nexus_browser_sessions table must be registered in "
            "InfrastructureManager.get_table_definitions()",
        )
        self.assertEqual(table.table_name, "nexus_browser_sessions")

    def test_browser_sessions_primary_key(self):
        table, _ = self._find_browser_sessions_table()
        self.assertIsNotNone(table)
        self.assertEqual(
            table.key_schema,
            [{"AttributeName": "session_id", "KeyType": "HASH"}],
            "Primary key must be session_id (HASH)",
        )

    def test_browser_sessions_ttl_attribute(self):
        table, _ = self._find_browser_sessions_table()
        self.assertIsNotNone(table)
        self.assertEqual(
            table.ttl_attribute,
            "expires_at",
            "TTL attribute must be 'expires_at' per design §9",
        )

    def test_browser_sessions_user_id_index_gsi(self):
        table, _ = self._find_browser_sessions_table()
        self.assertIsNotNone(table)
        self.assertIsNotNone(table.global_secondary_indexes)

        gsis_by_name = {g["IndexName"]: g for g in table.global_secondary_indexes}
        self.assertIn(
            "UserIdIndex", gsis_by_name,
            "GSI UserIdIndex must exist on nexus_browser_sessions",
        )
        user_id_index = gsis_by_name["UserIdIndex"]
        self.assertEqual(
            user_id_index["KeySchema"],
            [
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "last_used_at", "KeyType": "RANGE"},
            ],
            "UserIdIndex must be user_id (HASH) + last_used_at (RANGE)",
        )
        self.assertEqual(
            user_id_index["Projection"],
            {"ProjectionType": "ALL"},
        )

    def test_browser_sessions_attribute_definitions_cover_keys(self):
        """All attributes used in KeySchema + GSI KeySchemas must be declared
        in AttributeDefinitions as type 'S' (string)."""
        table, _ = self._find_browser_sessions_table()
        self.assertIsNotNone(table)
        attr_by_name = {a["AttributeName"]: a for a in table.attribute_definitions}
        for required in ("session_id", "user_id", "last_used_at"):
            self.assertIn(
                required, attr_by_name,
                f"AttributeDefinitions must include {required}",
            )
            self.assertEqual(attr_by_name[required]["AttributeType"], "S")

    def test_create_table_calls_ddb_with_expected_params(self):
        """Simulate `create_table()` against a mocked DDB client and assert:
        - create_table is called with correct KeySchema, AttrDefs, GSIs
        - update_time_to_live is called with AttributeName=expires_at
        """
        from botocore.exceptions import ClientError
        from nexus_utils.cli.managers.infrastructure_manager import (
            InfrastructureManager,
        )

        table, infra = self._find_browser_sessions_table()
        self.assertIsNotNone(table)

        mock_client = MagicMock()
        # First describe_table call raises ResourceNotFoundException (table does not exist yet)
        mock_client.describe_table.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}},
            "DescribeTable",
        )
        mock_client.create_table.return_value = {}
        mock_client.get_waiter.return_value = MagicMock(wait=MagicMock(return_value=None))
        mock_client.update_time_to_live.return_value = {}

        infra._dynamodb_client = mock_client

        created, status = infra.create_table(table)
        self.assertTrue(created)
        self.assertEqual(status, "created")

        # Verify create_table invocation
        mock_client.create_table.assert_called_once()
        call_kwargs = mock_client.create_table.call_args.kwargs
        self.assertEqual(call_kwargs["TableName"], "nexus_browser_sessions")
        self.assertEqual(
            call_kwargs["KeySchema"],
            [{"AttributeName": "session_id", "KeyType": "HASH"}],
        )
        self.assertEqual(call_kwargs["BillingMode"], "PAY_PER_REQUEST")
        gsi_names = [g["IndexName"] for g in call_kwargs["GlobalSecondaryIndexes"]]
        self.assertIn("UserIdIndex", gsi_names)

        # Verify TTL setup with expires_at
        mock_client.update_time_to_live.assert_called_once()
        ttl_kwargs = mock_client.update_time_to_live.call_args.kwargs
        self.assertEqual(ttl_kwargs["TableName"], "nexus_browser_sessions")
        self.assertEqual(
            ttl_kwargs["TimeToLiveSpecification"],
            {"Enabled": True, "AttributeName": "expires_at"},
        )

    def test_create_table_idempotent_when_exists(self):
        """If the table already exists, create_table() must return (False,
        'exists') without calling create_table on the DDB client (idempotent
        re-run of `./nexus-cli init`)."""
        from nexus_utils.cli.managers.infrastructure_manager import (
            InfrastructureManager,
        )

        table, infra = self._find_browser_sessions_table()
        self.assertIsNotNone(table)

        mock_client = MagicMock()
        # describe_table succeeds → table exists
        mock_client.describe_table.return_value = {"Table": {"TableName": table.table_name}}
        infra._dynamodb_client = mock_client

        created, status = infra.create_table(table)
        self.assertFalse(created)
        self.assertEqual(status, "exists")
        mock_client.create_table.assert_not_called()


class TestBrowserSessionsTableKeyInConfig(unittest.TestCase):
    """dynamodb.tables must include a 'browser_sessions' key so the short→full
    name mapping resolves to 'nexus_browser_sessions'."""

    def test_browser_sessions_in_dynamodb_tables_dict(self):
        from nexus_utils.config_loader import get_config
        config = get_config()
        ddb_config = config.get_dynamodb_config()
        tables = ddb_config.get("tables", {})
        self.assertIn(
            "browser_sessions", tables,
            "dynamodb.tables must contain 'browser_sessions' key",
        )
        # After prefix expansion, value should be 'nexus_browser_sessions'
        self.assertEqual(tables["browser_sessions"], "nexus_browser_sessions")


if __name__ == "__main__":
    unittest.main()
