# Use declarative resumable Tenant provisioning

`coengramctl tenant apply` validates and plans a Tenant Manifest, records provisioning state, invokes host-side Docker Compose for the isolated Tenant Memory Store, creates the tenant PostgreSQL database and role, initializes schemas, and activates the Tenant only after checks pass. Failures remain resumable rather than triggering destructive rollback, and no application container receives the Docker socket.
