"""Tests for per-sender channel routing overrides."""

from types import SimpleNamespace

from adapters.shared.handlers.webhooks import lookup_routing_override, normalize_sender_id


def _tenant(overrides: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(channel_routing_overrides=overrides))


def test_normalize_whatsapp_strips_suffix_and_nondigits() -> None:
    assert normalize_sender_id("whatsapp", "972523133649@s.whatsapp.net") == "972523133649"
    assert normalize_sender_id("whatsapp", "+972 52-313-3649") == "972523133649"


def test_normalize_telegram_strips_nondigits() -> None:
    assert normalize_sender_id("telegram", "123456789") == "123456789"


def test_normalize_teams_passthrough() -> None:
    assert normalize_sender_id("teams", "29:1abc-aad-object-id") == "29:1abc-aad-object-id"


def test_lookup_hit() -> None:
    t = _tenant({"whatsapp:972523133649": "voice_say"})
    assert lookup_routing_override(t, "whatsapp", "972523133649@s.whatsapp.net") == "voice_say"


def test_lookup_miss_wrong_channel() -> None:
    t = _tenant({"whatsapp:972523133649": "voice_say"})
    assert lookup_routing_override(t, "telegram", "972523133649") is None


def test_lookup_miss_wrong_sender() -> None:
    t = _tenant({"whatsapp:972523133649": "voice_say"})
    assert lookup_routing_override(t, "whatsapp", "972000000000@s.whatsapp.net") is None


def test_lookup_empty_overrides_returns_none() -> None:
    assert lookup_routing_override(_tenant({}), "whatsapp", "972523133649") is None


def test_lookup_missing_attribute_returns_none() -> None:
    """If the tenant settings predate the field, lookup must not crash."""
    t = SimpleNamespace(settings=SimpleNamespace())  # no channel_routing_overrides
    assert lookup_routing_override(t, "whatsapp", "972523133649") is None
