# 03. Admin Sessions and CSRF

Status: ready-for-agent

Blocked by: 02. Operator auth and bootstrap CLI.

## What to build

Exchange Operator Access Tokens for browser Admin Sessions using httpOnly cookies and
server-issued CSRF tokens for mutating requests.

## Acceptance criteria

- [ ] `POST /api/v1/admin/session` authenticates an Operator Access Token, creates an
      Admin Session, sets an httpOnly cookie, and returns Operator identity, roles, and
      CSRF token.
- [ ] `GET /api/v1/admin/session` returns current Admin Session context.
- [ ] `DELETE /api/v1/admin/session` revokes the Admin Session and clears the cookie.
- [ ] Admin Sessions expire after 30 minutes idle or 8 hours absolute.
- [ ] Mutating `/api/v1/admin/*` requests require a valid session-bound CSRF token.
- [ ] Safe cookie flags are configured for development and production contexts.
- [ ] Tests cover missing/expired/revoked sessions, idle expiry, absolute expiry, CSRF
      rejection, and CSRF success.

## Comments

No comments yet.
