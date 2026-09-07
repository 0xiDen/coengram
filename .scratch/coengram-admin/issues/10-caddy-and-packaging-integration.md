# 10. Caddy and packaging integration

Status: ready-for-agent

Blocked by: 07. Admin frontend scaffold; 08. Admin frontend workflows.

## What to build

Wire the admin frontend into local and production packaging so Caddy serves `/admin`
and proxies `/api/v1/*` to the gateway.

## Acceptance criteria

- [ ] Local development supports Vite dev server with backend proxy.
- [ ] Production build produces static admin assets.
- [ ] Caddy config serves `/admin` and admin static assets while retaining existing API,
      MCP, archive, and Telegram routing behavior.
- [ ] Gateway remains the only browser-facing admin API.
- [ ] Docker/Compose or Makefile changes build the admin frontend reproducibly.
- [ ] Deployment config tests cover `/admin` static routing and existing protected
      routes.

## Comments

No comments yet.
