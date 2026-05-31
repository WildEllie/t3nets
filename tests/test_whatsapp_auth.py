"""Unit tests for is_authorized_whatsapp_sender."""

from __future__ import annotations

import types

from adapters.shared.handlers.webhooks import is_authorized_whatsapp_sender


def _tenant(restrict: bool = True) -> types.SimpleNamespace:
    settings = types.SimpleNamespace(whatsapp_restrict_to_users=restrict)
    return types.SimpleNamespace(settings=settings)


def _user(whatsapp_identity: str | None) -> types.SimpleNamespace:
    identities: dict[str, str] = {}
    if whatsapp_identity is not None:
        identities["whatsapp"] = whatsapp_identity
    return types.SimpleNamespace(channel_identities=identities)


# ---------------------------------------------------------------------------
# Authorised cases
# ---------------------------------------------------------------------------


def test_exact_match_returns_true() -> None:
    tenant = _tenant()
    users = [_user("972545245926")]
    assert is_authorized_whatsapp_sender(tenant, users, "972545245926") is True


def test_sender_with_suffix_normalises_and_matches() -> None:
    """Inbound sender like '972545245926@s.whatsapp.net' is pre-normalised by
    normalize_sender_id before being passed here, so the stored bare number
    must still match."""
    tenant = _tenant()
    users = [_user("972545245926")]
    # Simulate what normalize_sender_id produces for '972545245926@s.whatsapp.net'
    sender_norm = "972545245926"
    assert is_authorized_whatsapp_sender(tenant, users, sender_norm) is True


def test_stored_value_with_plus_prefix_normalises_and_matches() -> None:
    """Stored identity like '+972545245926' should normalise to digits-only."""
    tenant = _tenant()
    users = [_user("+972545245926")]
    assert is_authorized_whatsapp_sender(tenant, users, "972545245926") is True


def test_stored_value_with_suffix_normalises_and_matches() -> None:
    """Stored identity can also carry an @suffix (e.g. copied from a chat ID)."""
    tenant = _tenant()
    users = [_user("972545245926@s.whatsapp.net")]
    assert is_authorized_whatsapp_sender(tenant, users, "972545245926") is True


def test_restriction_disabled_always_returns_true() -> None:
    """When whatsapp_restrict_to_users is False any sender is allowed."""
    tenant = _tenant(restrict=False)
    # Empty user list — should still pass because restriction is off.
    assert is_authorized_whatsapp_sender(tenant, [], "999000111") is True


# ---------------------------------------------------------------------------
# Unauthorised cases
# ---------------------------------------------------------------------------


def test_sender_not_in_user_list_returns_false() -> None:
    tenant = _tenant()
    users = [_user("972545245926")]
    assert is_authorized_whatsapp_sender(tenant, users, "972000000000") is False


def test_empty_user_list_returns_false() -> None:
    tenant = _tenant()
    assert is_authorized_whatsapp_sender(tenant, [], "972545245926") is False


def test_user_has_no_whatsapp_key_returns_false() -> None:
    tenant = _tenant()
    users = [_user(None)]  # channel_identities has no "whatsapp" key
    assert is_authorized_whatsapp_sender(tenant, users, "972545245926") is False


def test_multiple_users_wrong_number_returns_false() -> None:
    tenant = _tenant()
    users = [_user("111111111"), _user("222222222")]
    assert is_authorized_whatsapp_sender(tenant, users, "999999999") is False


def test_multiple_users_one_matches_returns_true() -> None:
    tenant = _tenant()
    users = [_user("111111111"), _user("972545245926"), _user("333333333")]
    assert is_authorized_whatsapp_sender(tenant, users, "972545245926") is True
