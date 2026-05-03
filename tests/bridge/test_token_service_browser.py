"""
Unit tests for TokenService.generate_browser_token (U0.1).

Browser tokens are user-level tokens (not connection-bound) with 8-hour default TTL.
They carry only {uid, type="browser", exp, nonce} — no cid/sid/aid fields.
"""
import time
import pytest

from nexus_utils.bridge.token_service import TokenService


def test_generate_browser_token_returns_valid_signed_token():
    """A generated browser token must verify successfully and have the expected payload."""
    svc = TokenService(secret_key="test-secret-u01")
    before_ts = int(time.time())

    token = svc.generate_browser_token(user_id="u1")
    ok, payload, err = svc.verify_token(token)

    assert ok is True, f"verify_token should succeed, got err={err}"
    assert err == ""
    assert payload is not None
    assert payload["type"] == "browser"
    assert payload["uid"] == "u1"
    assert payload["exp"] > before_ts, "exp must be a future timestamp"


def test_browser_token_does_not_bind_connection_id():
    """Browser tokens are user-level: they must NOT contain cid/sid/aid fields."""
    svc = TokenService(secret_key="test-secret-u01")
    token = svc.generate_browser_token(user_id="u1")
    ok, payload, _ = svc.verify_token(token)

    assert ok is True
    assert "cid" not in payload, "browser token must not bind connection_id"
    assert "sid" not in payload, "browser token must not bind session_id"
    assert "aid" not in payload, "browser token must not bind agent_id"


def test_browser_token_expires_after_ttl():
    """A browser token with short TTL must verify as expired after the TTL passes."""
    svc = TokenService(secret_key="test-secret-u01")
    token = svc.generate_browser_token(user_id="u1", expiry_seconds=1)

    time.sleep(2)

    ok, payload, err = svc.verify_token(token)
    assert ok is False
    assert payload is None
    assert err == "Token expired"


def test_browser_token_default_ttl_is_8_hours():
    """Default TTL must be 28800 seconds (8 hours), allow +/- 10s tolerance."""
    svc = TokenService(secret_key="test-secret-u01")
    now = int(time.time())
    token = svc.generate_browser_token(user_id="u1")
    ok, payload, _ = svc.verify_token(token)

    assert ok is True
    expected_exp = now + 28800
    assert abs(payload["exp"] - expected_exp) <= 10, (
        f"default exp should be ~now+28800 ({expected_exp}), got {payload['exp']}"
    )


def test_browser_token_signature_tampering_rejected():
    """If the token body is modified, signature verification must fail."""
    svc = TokenService(secret_key="test-secret-u01")
    token = svc.generate_browser_token(user_id="u1")

    # Flip a character somewhere in the middle to tamper the token.
    mid = len(token) // 2
    original_char = token[mid]
    # Pick a replacement char that's definitely different and still within the
    # urlsafe-base64 alphabet so decoding itself doesn't fail trivially.
    replacement = "A" if original_char != "A" else "B"
    tampered = token[:mid] + replacement + token[mid + 1:]
    assert tampered != token

    ok, payload, err = svc.verify_token(tampered)
    assert ok is False
    assert payload is None
    # Accept either a signature-rejection message or a broader verification error
    # (tampering may break base64/JSON decode depending on where we flip).
    assert "signature" in err.lower() or "verification" in err.lower() or "invalid" in err.lower(), (
        f"expected tampering to be rejected, got err={err!r}"
    )


def test_browser_token_has_nonce():
    """Two tokens generated with the same inputs must differ (nonce prevents replay)."""
    svc = TokenService(secret_key="test-secret-u01")
    token_a = svc.generate_browser_token(user_id="u1")
    token_b = svc.generate_browser_token(user_id="u1")

    assert token_a != token_b, "tokens with same inputs must differ due to nonce"

    _, payload_a, _ = svc.verify_token(token_a)
    _, payload_b, _ = svc.verify_token(token_b)
    assert payload_a["nonce"] != payload_b["nonce"]
