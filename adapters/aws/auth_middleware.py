"""
Authentication middleware for AWS deployment.

Extracts user identity from JWT claims passed by API Gateway's JWT authorizer.
API Gateway validates the token signature and expiry — this middleware just
reads the forwarded claims from headers.

Headers set by API Gateway JWT authorizer context:
  - Authorization: Bearer {jwt}  (original token)

The JWT id_token from Cognito contains:
  - sub: Cognito user ID
  - email: User email
  - custom:tenant_id: Tenant ID (custom Cognito attribute)
"""

import base64
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("t3nets.auth")


@dataclass
class AuthContext:
    """Extracted authentication context from JWT."""

    user_id: str       # Cognito sub
    tenant_id: str     # custom:tenant_id claim
    email: str = ""    # email claim


class AuthError(Exception):
    """Authentication or authorization failure."""

    def __init__(self, message: str, status: int = 401):
        self.message = message
        self.status = status
        super().__init__(message)


def extract_auth(headers) -> AuthContext:
    """Extract auth context from the Authorization header.

    The JWT has already been validated by API Gateway's JWT authorizer.
    We just decode the payload to read claims. No signature verification
    needed — API Gateway already did that.

    Args:
        headers: HTTP headers (BaseHTTPRequestHandler.headers)

    Returns:
        AuthContext with user_id, tenant_id, email

    Raises:
        AuthError: If no valid auth is present
    """
    auth_header = headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise AuthError("Missing or invalid Authorization header")

    token = auth_header[7:]  # strip "Bearer "

    try:
        # Decode JWT payload (second segment) — no verification needed,
        # API Gateway already validated signature
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError("Malformed JWT")

        # Base64-decode payload (add padding if needed)
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding

        payload = json.loads(base64.urlsafe_b64decode(payload_b64))

        user_id = payload.get("sub", "")
        tenant_id = payload.get("custom:tenant_id", "")
        email = payload.get("email", "")

        if not user_id:
            raise AuthError("JWT missing 'sub' claim")
        if not tenant_id:
            raise AuthError("JWT missing 'custom:tenant_id' claim", 403)

        logger.info(f"Auth: user={user_id[:8]}... tenant={tenant_id} email={email}")
        return AuthContext(user_id=user_id, tenant_id=tenant_id, email=email)

    except AuthError:
        raise
    except Exception as e:
        logger.warning(f"JWT decode error: {e}")
        raise AuthError(f"Invalid token: {e}")
