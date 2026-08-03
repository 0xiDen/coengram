ARG POSTGRES_VERSION=17.6-alpine
ARG POSTGRES_DIGEST=sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94

FROM postgres:${POSTGRES_VERSION}@${POSTGRES_DIGEST} AS control

# The upstream entrypoint sources non-executable .sh files. Copying them into the
# image avoids host bind-mount execution differences while keeping secrets runtime-only.
COPY --chmod=0555 entrypoint.sh /usr/local/bin/coengram-postgres-entrypoint
COPY --chmod=0444 init-control.sh /docker-entrypoint-initdb.d/10-memory-control.sh
ENTRYPOINT ["/usr/local/bin/coengram-postgres-entrypoint"]
CMD ["postgres"]

FROM control AS local

COPY --chmod=0444 init-tenants.sh /docker-entrypoint-initdb.d/20-memory-tenant.sh
