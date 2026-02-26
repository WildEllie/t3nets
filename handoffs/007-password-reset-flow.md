# Handoff: Password Reset Flow

**Date:** 2026-02-25
**Status:** Completed
**Roadmap Item:** Phase 2 Multi-Tenancy → Password reset support

## What Was Done

Added a complete password reset flow (forgot password → verification code → new password). This was triggered by seeded Cognito users hitting `PasswordResetRequiredException` when trying to log in — the login handler didn't catch this exception and dumped the raw error to the frontend.

Three changes:
1. **Backend:** Two new endpoints (`POST /api/auth/forgot-password` and `POST /api/auth/confirm-reset`) plus `PasswordResetRequiredException` handling in the login endpoint
2. **Frontend:** Two new panels (`forgotPanel` and `resetPanel`) in `login.html`, "Forgot password?" link on login page, auto-redirect to reset flow when Cognito requires it
3. **Terraform:** Two new public API Gateway routes (no JWT authorizer)

## Key Files Changed

| File | What Changed |
|------|-------------|
| `adapters/aws/server.py` | Added `PasswordResetRequiredException` → 403 with `PASSWORD_RESET_REQUIRED` code in login handler. Added `_handle_auth_forgot_password()` (calls `client.forgot_password()`) and `_handle_auth_confirm_reset()` (calls `client.confirm_forgot_password()`). Wired both into `do_POST` routing. |
| `adapters/local/login.html` | Added `forgotPanel` (email input → send code) and `resetPanel` (code + new password). Added `handleForgotPassword()` and `handleConfirmReset()` JS handlers. Login error handler auto-triggers forgot-password when `PASSWORD_RESET_REQUIRED` code received. Enter key support for new inputs. |
| `infra/aws/modules/api/main.tf` | Added `POST /api/auth/forgot-password` and `POST /api/auth/confirm-reset` as public routes (no JWT). |

## Architecture & Design Decisions

### Auto-redirect on PasswordResetRequiredException

When a user logs in and Cognito returns `PasswordResetRequiredException`, the login handler returns 403 with `code: "PASSWORD_RESET_REQUIRED"`. The frontend detects this, pre-fills the email, and automatically calls `handleForgotPassword()` — triggering Cognito to send the code and showing the reset panel. The user doesn't have to click "Forgot password?" manually.

### Don't leak email existence

The `forgot-password` endpoint always returns `{"message": "Reset code sent"}` regardless of whether the email exists. `UserNotFoundException` and `InvalidParameterException` are caught and return the same success response. This prevents email enumeration attacks.

### Two-panel flow vs single panel

Split into two panels (forgotPanel → resetPanel) rather than one combined panel. This matches how email verification already works (separate panels for request and confirm), and keeps each step simple.

## Current State

- **What works:** Full forgot-password flow: enter email → receive code → enter code + new password → login. Auto-redirect from `PasswordResetRequiredException`. All error cases handled (invalid code, expired code, weak password, rate limiting).
- **What doesn't yet:** No "resend code" button on the reset panel. No password strength indicator.
- **Known issues:** None discovered.

## How to Pick Up From Here

### Deploy

```bash
cd infra/aws && terraform apply -var-file=environments/dev.tfvars
cd ../.. && ./scripts/deploy.sh
```

### Test scenarios

1. **Manual forgot password:** `/login` → "Forgot password?" → enter email → check inbox → enter code + new password → sign in
2. **Forced reset:** In AWS Console, run `admin-reset-user-password` for a user → user tries to login → auto-redirects to reset flow
3. **Error cases:** wrong code, expired code, weak password, non-existent email (should still show "code sent")

## Dependencies & Gotchas

- **Cognito SES integration:** For forgot-password emails to actually send, Cognito needs SES configured or must be out of the sandbox. In sandbox mode, only verified email addresses receive emails.
- **Rate limiting:** Cognito has built-in rate limits on `forgot_password` calls. The handler returns 429 for `LimitExceededException`.
- **The `forgot-password` endpoint is unauthenticated** (no JWT) by design — users who forgot their password obviously can't provide a token. This is the same pattern as login/signup/confirm.
