ARG CADDY_VERSION=2.10.2
ARG CADDY_BUILDER_DIGEST=sha256:4cff3ae272ea05842adb4546e35961e4947aeea65f80fa1eb62a35b960e84971
ARG CADDY_RUNTIME_DIGEST=sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d
ARG CADDY_CLOUDFLARE_COMMIT=a8737d095ad5a48ca031cea6ab704057dbc2d250

FROM caddy:${CADDY_VERSION}-builder-alpine@${CADDY_BUILDER_DIGEST} AS builder

ARG CADDY_CLOUDFLARE_COMMIT

RUN xcaddy build \
    --with github.com/caddy-dns/cloudflare@${CADDY_CLOUDFLARE_COMMIT}

FROM caddy:${CADDY_VERSION}-alpine@${CADDY_RUNTIME_DIGEST}

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
COPY Caddyfile Caddyfile.test gateway-routes.caddy /etc/caddy/
COPY entrypoint.sh /usr/local/bin/memory-caddy-entrypoint
RUN chmod 0555 /usr/local/bin/memory-caddy-entrypoint

ENTRYPOINT ["/usr/local/bin/memory-caddy-entrypoint"]
CMD ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
