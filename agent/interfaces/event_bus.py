"""
Re-export shim — canonical definitions live in t3nets_sdk.interfaces.event_bus.

Kept for backwards-compatible imports of the form:
    from agent.interfaces.event_bus import EventBus
"""

from t3nets_sdk.interfaces.event_bus import EventBus

__all__ = ["EventBus"]
