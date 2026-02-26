"""
Error handler tests.

Verifies that known error patterns are matched to friendly, actionable messages.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.errors.handler import ErrorHandler


@pytest.fixture
def handler():
    return ErrorHandler()


# --- Bedrock errors ---


def test_service_unavailable_exception(handler):
    """ServiceUnavailableException should map to BEDROCK_SERVICE_UNAVAILABLE."""
    error = Exception(
        "An error occurred (ServiceUnavailableException) when calling the "
        "Converse operation: Service unavailable"
    )
    result = handler.handle(error, context="chat")
    assert result.error_code == "BEDROCK_SERVICE_UNAVAILABLE"
    assert "Bedrock" in result.message
    assert result.admin_required is True


def test_service_unavailable_string(handler):
    """Plain 'Service Unavailable' string should also match."""
    result = handler.handle_string("Service Unavailable", context="chat")
    assert result.error_code == "BEDROCK_SERVICE_UNAVAILABLE"


def test_inference_profile_error(handler):
    """On-demand throughput error should map to BEDROCK_INFERENCE_PROFILE."""
    error = Exception(
        "Invocation of model ID anthropic.claude-sonnet-4-5-20250929-v1:0 "
        "with on-demand throughput isn't supported"
    )
    result = handler.handle(error, context="chat")
    assert result.error_code == "BEDROCK_INFERENCE_PROFILE"


def test_model_access_denied(handler):
    """Model access denied should map to BEDROCK_MODEL_ACCESS."""
    error = Exception(
        "Model access is denied due to IAM user or service role is not authorized"
    )
    result = handler.handle(error, context="chat")
    assert result.error_code == "BEDROCK_MODEL_ACCESS"


def test_iam_denied(handler):
    """AccessDeniedException for bedrock:InvokeModel should map to BEDROCK_IAM_DENIED."""
    error = Exception(
        "AccessDeniedException: User is not authorized to perform bedrock:InvokeModel"
    )
    result = handler.handle(error, context="chat")
    assert result.error_code == "BEDROCK_IAM_DENIED"


def test_throttling(handler):
    """ThrottlingException should map to THROTTLED."""
    error = Exception("ThrottlingException: Rate exceeded")
    result = handler.handle(error, context="chat")
    assert result.error_code == "THROTTLED"


def test_model_timeout(handler):
    """ModelTimeoutException should map to MODEL_TIMEOUT."""
    error = Exception("ModelTimeoutException: Request timed out")
    result = handler.handle(error, context="chat")
    assert result.error_code == "MODEL_TIMEOUT"


def test_validation_max_tokens(handler):
    """ValidationException with max_tokens should map to MAX_TOKENS_EXCEEDED."""
    error = Exception("ValidationException: max_tokens must be less than 4096")
    result = handler.handle(error, context="chat")
    assert result.error_code == "MAX_TOKENS_EXCEEDED"


def test_validation_generic(handler):
    """Generic ValidationException should map to BEDROCK_VALIDATION."""
    error = Exception("ValidationException: Invalid model configuration")
    result = handler.handle(error, context="chat")
    assert result.error_code == "BEDROCK_VALIDATION"


# --- Fallback ---


def test_unknown_error_falls_to_generic(handler):
    """Unrecognized errors should fall through to GENERIC_ERROR."""
    error = Exception("Something completely unexpected happened")
    result = handler.handle(error, context="chat")
    assert result.error_code == "UNKNOWN"
    assert "Something unexpected went wrong" in result.message
