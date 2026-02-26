"""
Bot Framework Authentication — JWT validation and token acquisition.

Handles two auth flows:
1. Inbound: Validate JWTs from Microsoft Bot Framework on incoming webhooks
2. Outbound: Acquire Bearer tokens to send responses back via Bot Framework API

Uses Microsoft's OpenID Connect metadata for key discovery.
No external SDK dependency — pure HTTP + PyJWT.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.request import urlopen, Request

logger = logging.getLogger("t3nets.teams.auth")

# Microsoft Bot Framework OpenID metadata endpoints
BOT_FRAMEWORK_OPENID_URL = (
    "https://login.botframework.com/v1/.well-known/openid-configuration"
)
EMULATOR_OPENID_URL = (
    "https://login.microsoftonline.com/botframework.com/v2.0/"
    ".well-known/openid-configuration"
)

# Token endpoint for outbound bot-to-user messages
BOT_TOKEN_URL = (
    "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
)
BOT_TOKEN_SCOPE = "https://api.botframework.com/.default"

# Valid issuers for Bot Framework JWTs
VALID_ISSUERS = [
    "https://api.botframework.com",
    "https://sts.windows.net/d6d49420-f39b-4df7-a1dc-d59a935871db/",
    "https://login.microsoftonline.com/d6d49420-f39b-4df7-a1dc-d59a935871db/v2.0",
    "https://sts.windows.net/f8cdef31-a31e-4b4a-93e4-5f571e91255a/",
    "https://login.microsoftonline.com/f8cdef31-a31e-4b4a-93e4-5f571e91255a/v2.0",
]


@dataclass
class TokenCache:
    """Cached OAuth token with expiry tracking."""

    access_token: str = ""
    expires_at: float = 0.0

    def is_valid(self) -> bool:
        """Check if token is still valid (with 5-minute buffer)."""
        return self.access_token and time.time() < (self.expires_at - 300)


@dataclass
class SigningKeyCache:
    """Cached JWKS signing keys with refresh tracking."""

    keys: list[dict] = field(default_factory=list)
    fetched_at: float = 0.0
    max_age: float = 86400  # Refresh keys every 24 hours

    def is_fresh(self) -> bool:
        return self.keys and (time.time() - self.fetched_at) < self.max_age


class BotFrameworkAuth:
    """
    Handles Microsoft Bot Framework authentication.

    - Validates incoming webhook JWTs (signed by Microsoft)
    - Acquires outbound tokens for sending responses to Teams
    """

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token_cache = TokenCache()
        self._key_cache = SigningKeyCache()

    # --- Inbound: Validate incoming webhook JWT ---

    def validate_incoming(self, auth_header: str) -> bool:
        """
        Validate the Authorization header from a Bot Framework webhook.

        Args:
            auth_header: The full Authorization header value (e.g., "Bearer eyJ...")

        Returns:
            True if the JWT is valid, False otherwise.
        """
        if not auth_header or not auth_header.startswith("Bearer "):
            logger.warning("Missing or malformed Authorization header")
            return False

        token = auth_header[7:]  # Strip "Bearer "

        try:
            # Decode header without verification to get kid
            header = self._decode_jwt_header(token)
            kid = header.get("kid", "")

            if not kid:
                logger.warning("JWT missing 'kid' header")
                return False

            # Get signing keys
            keys = self._get_signing_keys()
            matching_key = next((k for k in keys if k.get("kid") == kid), None)

            if not matching_key:
                # Keys might have rotated — force refresh
                self._key_cache = SigningKeyCache()
                keys = self._get_signing_keys()
                matching_key = next((k for k in keys if k.get("kid") == kid), None)

            if not matching_key:
                logger.warning(f"No matching signing key for kid={kid}")
                return False

            # Decode and validate the JWT
            payload = self._decode_and_validate_jwt(token, matching_key)
            if not payload:
                return False

            # Validate claims
            issuer = payload.get("iss", "")
            audience = payload.get("aud", "")
            exp = payload.get("exp", 0)

            if issuer not in VALID_ISSUERS:
                logger.warning(f"Invalid issuer: {issuer}")
                return False

            if audience != self.app_id:
                logger.warning(f"Invalid audience: {audience} (expected {self.app_id})")
                return False

            if exp < time.time():
                logger.warning("Token expired")
                return False

            logger.debug("Bot Framework JWT validated successfully")
            return True

        except Exception as e:
            logger.error(f"JWT validation error: {e}")
            return False

    def _get_signing_keys(self) -> list[dict]:
        """Fetch Microsoft's signing keys from OpenID metadata."""
        if self._key_cache.is_fresh():
            return self._key_cache.keys

        try:
            # Fetch OpenID configuration
            req = Request(BOT_FRAMEWORK_OPENID_URL)
            with urlopen(req, timeout=10) as resp:
                config = json.loads(resp.read().decode())

            jwks_uri = config.get("jwks_uri", "")
            if not jwks_uri:
                logger.error("No jwks_uri in OpenID config")
                return []

            # Fetch JWKS
            req = Request(jwks_uri)
            with urlopen(req, timeout=10) as resp:
                jwks = json.loads(resp.read().decode())

            keys = jwks.get("keys", [])
            self._key_cache = SigningKeyCache(keys=keys, fetched_at=time.time())
            logger.info(f"Fetched {len(keys)} signing keys from Bot Framework")
            return keys

        except Exception as e:
            logger.error(f"Failed to fetch signing keys: {e}")
            return self._key_cache.keys  # Return stale keys as fallback

    def _decode_jwt_header(self, token: str) -> dict:
        """Decode JWT header without verification (to get kid)."""
        import base64

        header_b64 = token.split(".")[0]
        # Add padding
        padding = 4 - len(header_b64) % 4
        if padding != 4:
            header_b64 += "=" * padding
        header_bytes = base64.urlsafe_b64decode(header_b64)
        return json.loads(header_bytes)

    def _decode_and_validate_jwt(
        self, token: str, signing_key: dict
    ) -> Optional[dict]:
        """
        Decode and validate a JWT using the provided signing key.

        For production, this uses PyJWT if available. Falls back to
        manual validation for environments without PyJWT.
        """
        try:
            import jwt  # PyJWT

            # Build the public key from JWK
            from jwt.algorithms import RSAAlgorithm

            public_key = RSAAlgorithm.from_jwk(json.dumps(signing_key))

            payload = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                options={
                    "verify_aud": False,  # We check audience manually
                    "verify_iss": False,  # We check issuer manually
                },
            )
            return payload

        except ImportError:
            # PyJWT not installed — do basic decode without signature verification
            # WARNING: This is NOT secure for production. Install PyJWT.
            logger.warning(
                "PyJWT not installed — JWT signature NOT verified. "
                "Install PyJWT for production: pip install PyJWT[crypto]"
            )
            return self._unsafe_decode_jwt_payload(token)

        except Exception as e:
            logger.error(f"JWT decode failed: {e}")
            return None

    def _unsafe_decode_jwt_payload(self, token: str) -> Optional[dict]:
        """Decode JWT payload WITHOUT signature verification. Dev/testing only."""
        import base64

        try:
            payload_b64 = token.split(".")[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            return json.loads(payload_bytes)
        except Exception as e:
            logger.error(f"Failed to decode JWT payload: {e}")
            return None

    # --- Outbound: Get token for sending responses to Teams ---

    async def get_bot_token(self) -> str:
        """
        Acquire a Bearer token for sending messages back to Teams.

        Uses OAuth 2.0 client credentials flow against Microsoft's
        Bot Framework token endpoint. Tokens are cached until expiry.

        Returns:
            Bearer token string, or empty string on failure.
        """
        if self._token_cache.is_valid():
            return self._token_cache.access_token

        try:
            import urllib.parse

            data = urllib.parse.urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": self.app_id,
                    "client_secret": self.app_secret,
                    "scope": BOT_TOKEN_SCOPE,
                }
            ).encode()

            req = Request(
                BOT_TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )

            with urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())

            access_token = result.get("access_token", "")
            expires_in = result.get("expires_in", 3600)

            if access_token:
                self._token_cache = TokenCache(
                    access_token=access_token,
                    expires_at=time.time() + expires_in,
                )
                logger.info(
                    f"Acquired Bot Framework token (expires in {expires_in}s)"
                )

            return access_token

        except Exception as e:
            logger.error(f"Failed to acquire bot token: {e}")
            return ""

    # --- Utility ---

    def validate_emulator(self, auth_header: str) -> bool:
        """
        Validate a JWT from Bot Framework Emulator (local testing).
        More permissive — accepts emulator-specific issuers.
        """
        if not auth_header:
            # Emulator can run without auth in some modes
            logger.info("No auth header — assuming Bot Framework Emulator (no auth)")
            return True

        # For emulator, we do basic validation but accept more issuers
        return self.validate_incoming(auth_header)
