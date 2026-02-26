"""
Friendly error models.

Every user-facing error should be a FriendlyError — clear, actionable, warm.
"""

from dataclasses import dataclass
from enum import Enum


class ErrorSeverity(str, Enum):
    """How serious the error is and who can fix it."""

    INFO = "info"          # Transient — retry likely works (throttling, timeouts)
    CONFIG = "config"      # Configuration issue — admin action needed
    CRITICAL = "critical"  # Infrastructure issue — deployment action needed


@dataclass
class FriendlyError:
    """A user-facing error with context and guidance."""

    message: str                          # Friendly message for the user
    severity: ErrorSeverity               # info / config / critical
    error_code: str = ""                  # Machine-readable code (e.g. BEDROCK_MODEL_ACCESS)
    action: str = ""                      # What to do about it
    admin_required: bool = False          # Does an admin need to fix this?
    original_error: str = ""              # Raw error (logged, never shown to user)

    def to_dict(self) -> dict:
        return {
            "type": "error",
            "severity": self.severity.value,
            "message": self.message,
            "action": self.action,
            "admin_required": self.admin_required,
            "error_code": self.error_code,
        }
