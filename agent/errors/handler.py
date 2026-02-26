"""
ErrorHandler — catches exceptions and returns friendly, actionable messages.

Usage:
    from agent.errors.handler import ErrorHandler

    error_handler = ErrorHandler()

    try:
        result = do_something_risky()
    except Exception as e:
        friendly = error_handler.handle(e)
        return {"error": friendly.message, **friendly.to_dict()}
"""

import logging
from dataclasses import replace

from agent.errors.models import FriendlyError, ErrorSeverity
from agent.errors.catalog import ERROR_PATTERNS, GENERIC_ERROR

logger = logging.getLogger("t3nets.errors")


class ErrorHandler:
    """Matches exceptions against the error catalog and returns friendly messages."""

    def handle(self, error: Exception, context: str = "") -> FriendlyError:
        """Match an exception to a friendly error message.

        Args:
            error: The caught exception.
            context: Optional context string (e.g. "chat", "skill:sprint_status").

        Returns:
            A FriendlyError with a user-facing message and metadata.
        """
        error_str = str(error)

        # Try to match against known patterns
        for pattern, template in ERROR_PATTERNS:
            if pattern.search(error_str):
                friendly = replace(template, original_error=error_str)
                self._log_error(friendly, context)
                return friendly

        # No match — use generic fallback
        friendly = replace(GENERIC_ERROR, original_error=error_str)
        self._log_error(friendly, context, matched=False)
        return friendly

    def handle_string(self, error_message: str, context: str = "") -> FriendlyError:
        """Match a raw error string (not an exception) to a friendly error.

        Useful when the error comes from a JSON response or external API.
        """
        for pattern, template in ERROR_PATTERNS:
            if pattern.search(error_message):
                friendly = replace(template, original_error=error_message)
                self._log_error(friendly, context)
                return friendly

        friendly = replace(GENERIC_ERROR, original_error=error_message)
        self._log_error(friendly, context, matched=False)
        return friendly

    def _log_error(
        self, friendly: FriendlyError, context: str, matched: bool = True
    ) -> None:
        """Log the error with full details (never shown to user)."""
        prefix = f"[{context}] " if context else ""
        match_tag = friendly.error_code if matched else "UNMATCHED"

        if friendly.severity == ErrorSeverity.CRITICAL:
            logger.error(
                f"{prefix}{match_tag}: {friendly.original_error}"
            )
        elif friendly.severity == ErrorSeverity.CONFIG:
            logger.warning(
                f"{prefix}{match_tag}: {friendly.original_error}"
            )
        else:
            logger.info(
                f"{prefix}{match_tag}: {friendly.original_error}"
            )
