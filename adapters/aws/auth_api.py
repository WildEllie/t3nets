"""
T3nets Auth API — Cognito-backed authentication endpoints.

Handles login, signup, confirmation, token refresh, and password reset.
Reads Cognito configuration from environment at import time.
"""

import logging
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from adapters.aws.auth_middleware import AuthError, extract_auth

logger = logging.getLogger("t3nets.auth")

DEFAULT_TENANT = "default"

COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_APP_CLIENT_ID = os.environ.get("COGNITO_APP_CLIENT_ID", "")
COGNITO_AUTH_DOMAIN = os.environ.get("COGNITO_AUTH_DOMAIN", "")
WS_API_ENDPOINT = os.environ.get("WS_API_ENDPOINT", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


class AuthAPI:
    """Cognito-backed authentication handlers."""

    def __init__(self, tenants: Any) -> None:
        self.tenants = tenants

    async def config(self, request: Request) -> Response:
        response: dict[str, Any] = {
            "enabled": bool(COGNITO_USER_POOL_ID),
            "client_id": COGNITO_APP_CLIENT_ID,
            "auth_domain": COGNITO_AUTH_DOMAIN,
            "user_pool_id": COGNITO_USER_POOL_ID,
        }
        if WS_API_ENDPOINT:
            response["ws_endpoint"] = WS_API_ENDPOINT
        return JSONResponse(response)

    async def me(self, request: Request) -> Response:
        if not COGNITO_USER_POOL_ID:
            return JSONResponse(
                {
                    "authenticated": False,
                    "tenant_id": DEFAULT_TENANT,
                    "tenant_status": "active",
                }
            )
        try:
            auth = extract_auth(request.headers)
            email = auth.email
            tenant_id = ""
            display_name = ""
            avatar_url = ""
            role = "member"
            tenant_status = "onboarding"

            try:
                user = await self.tenants.get_user_by_cognito_sub(auth.user_id)
                if user:
                    tenant_id = user.tenant_id
                    email = email or user.email
                    display_name = user.display_name
                    avatar_url = user.avatar_url
                    role = user.role
                    logger.info(
                        f"auth/me: resolved tenant '{tenant_id}' from DynamoDB "
                        f"for sub {auth.user_id[:8]}..."
                    )
            except Exception as e:
                logger.warning(f"auth/me DynamoDB lookup failed: {e}")

            tenant_name = ""
            if tenant_id:
                try:
                    tenant = await self.tenants.get_tenant(tenant_id)
                    tenant_status = tenant.status
                    tenant_name = tenant.name
                except Exception:
                    tenant_status = "active"

            return JSONResponse(
                {
                    "authenticated": True,
                    "user_id": auth.user_id,
                    "tenant_id": tenant_id,
                    "email": email,
                    "role": role,
                    "display_name": display_name,
                    "avatar_url": avatar_url,
                    "tenant_status": tenant_status,
                    "tenant_name": tenant_name,
                }
            )
        except AuthError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)

    async def login(self, request: Request) -> Response:
        try:
            body = await request.json()
            email = body.get("email", "").strip()
            password = body.get("password", "")
            if not email or not password:
                return JSONResponse({"error": "Email and password are required"}, status_code=400)
            if not COGNITO_USER_POOL_ID or not COGNITO_APP_CLIENT_ID:
                return JSONResponse({"error": "Auth not configured"}, status_code=500)

            import boto3  # type: ignore[import-untyped]

            client = boto3.client("cognito-idp", region_name=AWS_REGION)
            result = client.initiate_auth(
                ClientId=COGNITO_APP_CLIENT_ID,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": email, "PASSWORD": password},
            )
            challenge = result.get("ChallengeName", "")
            if challenge:
                logger.warning(f"Auth login challenge: {challenge} for {email}")
                return JSONResponse(
                    {"error": f"Account requires action: {challenge}", "code": challenge},
                    status_code=403,
                )
            auth_result = result.get("AuthenticationResult", {})
            return JSONResponse(
                {
                    "id_token": auth_result.get("IdToken", ""),
                    "access_token": auth_result.get("AccessToken", ""),
                    "refresh_token": auth_result.get("RefreshToken", ""),
                }
            )
        except Exception as e:
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if err_code == "NotAuthorizedException":
                return JSONResponse({"error": "Invalid email or password"}, status_code=401)
            elif err_code == "UserNotConfirmedException":
                return JSONResponse(
                    {"error": "Email not verified", "code": "USER_NOT_CONFIRMED"}, status_code=403
                )
            elif err_code == "UserNotFoundException":
                return JSONResponse({"error": "Invalid email or password"}, status_code=401)
            elif err_code == "PasswordResetRequiredException":
                return JSONResponse(
                    {"error": "Password reset required", "code": "PASSWORD_RESET_REQUIRED"},
                    status_code=403,
                )
            else:
                logger.exception("Auth login error")
                return JSONResponse({"error": str(e)}, status_code=500)

    async def signup(self, request: Request) -> Response:
        try:
            body = await request.json()
            email = body.get("email", "").strip()
            password = body.get("password", "")
            name = body.get("name", "").strip()
            if not email or not password:
                return JSONResponse({"error": "Email and password are required"}, status_code=400)
            if not COGNITO_USER_POOL_ID or not COGNITO_APP_CLIENT_ID:
                return JSONResponse({"error": "Auth not configured"}, status_code=500)

            import boto3

            client = boto3.client("cognito-idp", region_name=AWS_REGION)
            user_attrs = [
                {"Name": "email", "Value": email},
                {"Name": "name", "Value": name or email.split("@")[0]},
            ]
            result = client.sign_up(
                ClientId=COGNITO_APP_CLIENT_ID,
                Username=email,
                Password=password,
                UserAttributes=user_attrs,
            )
            return JSONResponse(
                {
                    "user_sub": result.get("UserSub", ""),
                    "confirmed": result.get("UserConfirmed", False),
                },
                status_code=201,
            )
        except Exception as e:
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if err_code == "UsernameExistsException":
                return JSONResponse(
                    {"error": "An account with this email already exists"}, status_code=409
                )
            elif err_code == "InvalidPasswordException":
                msg = getattr(e, "response", {}).get("Error", {}).get("Message", str(e))
                return JSONResponse({"error": msg}, status_code=400)
            else:
                logger.exception("Auth signup error")
                return JSONResponse({"error": str(e)}, status_code=500)

    async def confirm(self, request: Request) -> Response:
        try:
            body = await request.json()
            email = body.get("email", "").strip()
            code = body.get("code", "").strip()
            if not email or not code:
                return JSONResponse({"error": "Email and code are required"}, status_code=400)
            if not COGNITO_APP_CLIENT_ID:
                return JSONResponse({"error": "Auth not configured"}, status_code=500)

            import boto3

            client = boto3.client("cognito-idp", region_name=AWS_REGION)
            client.confirm_sign_up(
                ClientId=COGNITO_APP_CLIENT_ID, Username=email, ConfirmationCode=code
            )
            return JSONResponse({"ok": True})
        except Exception as e:
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if err_code == "CodeMismatchException":
                return JSONResponse({"error": "Invalid verification code"}, status_code=400)
            elif err_code == "ExpiredCodeException":
                return JSONResponse({"error": "Verification code has expired"}, status_code=400)
            else:
                logger.exception("Auth confirm error")
                return JSONResponse({"error": str(e)}, status_code=500)

    async def refresh(self, request: Request) -> Response:
        try:
            body = await request.json()
            refresh_token = body.get("refresh_token", "")
            if not refresh_token:
                return JSONResponse({"error": "refresh_token is required"}, status_code=400)
            if not COGNITO_APP_CLIENT_ID:
                return JSONResponse({"error": "Auth not configured"}, status_code=500)

            import boto3

            client = boto3.client("cognito-idp", region_name=AWS_REGION)
            result = client.initiate_auth(
                ClientId=COGNITO_APP_CLIENT_ID,
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters={"REFRESH_TOKEN": refresh_token},
            )
            auth_result = result.get("AuthenticationResult", {})
            return JSONResponse(
                {
                    "id_token": auth_result.get("IdToken", ""),
                    "access_token": auth_result.get("AccessToken", ""),
                }
            )
        except Exception as e:
            logger.exception("Auth refresh error")
            return JSONResponse({"error": str(e)}, status_code=500)

    async def forgot_password(self, request: Request) -> Response:
        try:
            body = await request.json()
            email = body.get("email", "").strip()
            if not email:
                return JSONResponse({"error": "Email is required"}, status_code=400)
            if not COGNITO_APP_CLIENT_ID:
                return JSONResponse({"error": "Auth not configured"}, status_code=500)

            import boto3

            client = boto3.client("cognito-idp", region_name=AWS_REGION)
            client.forgot_password(ClientId=COGNITO_APP_CLIENT_ID, Username=email)
            return JSONResponse({"message": "Reset code sent"})
        except Exception as e:
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if err_code in ("UserNotFoundException", "InvalidParameterException"):
                return JSONResponse({"message": "Reset code sent"})
            elif err_code == "LimitExceededException":
                return JSONResponse(
                    {"error": "Too many attempts. Please try again later."}, status_code=429
                )
            else:
                logger.exception("Auth forgot-password error")
                return JSONResponse({"error": str(e)}, status_code=500)

    async def confirm_reset(self, request: Request) -> Response:
        try:
            body = await request.json()
            email = body.get("email", "").strip()
            code = body.get("code", "").strip()
            new_password = body.get("new_password", "")
            if not email or not code or not new_password:
                return JSONResponse(
                    {"error": "Email, code, and new password are required"}, status_code=400
                )
            if not COGNITO_APP_CLIENT_ID:
                return JSONResponse({"error": "Auth not configured"}, status_code=500)

            import boto3

            client = boto3.client("cognito-idp", region_name=AWS_REGION)
            client.confirm_forgot_password(
                ClientId=COGNITO_APP_CLIENT_ID,
                Username=email,
                ConfirmationCode=code,
                Password=new_password,
            )
            return JSONResponse({"message": "Password reset successful"})
        except Exception as e:
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if err_code == "CodeMismatchException":
                return JSONResponse({"error": "Invalid verification code"}, status_code=400)
            elif err_code == "ExpiredCodeException":
                return JSONResponse({"error": "Verification code has expired"}, status_code=400)
            elif err_code == "InvalidPasswordException":
                msg = getattr(e, "response", {}).get("Error", {}).get("Message", str(e))
                return JSONResponse({"error": msg}, status_code=400)
            else:
                logger.exception("Auth confirm-reset error")
                return JSONResponse({"error": str(e)}, status_code=500)
