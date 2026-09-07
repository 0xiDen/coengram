# Serve the admin panel through Caddy

The React Vite Chakra admin panel will be served by Caddy at `/admin`, while the browser calls a single gateway admin API under `/api/v1/admin/*`. This keeps production edge routing in one place, lets the gateway remain focused on typed HTTP behavior, and avoids exposing the Operator Service directly to browsers.
