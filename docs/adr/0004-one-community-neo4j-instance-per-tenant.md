# Isolate each Tenant in its own Neo4j Community instance

Each Tenant receives an independent Neo4j Community container, credentials, and persistent volume, and the authenticated gateway resolves the correct Bolt backend from the Tenant Session. Community Edition cannot create a database per Tenant, so this design accepts higher resource and operational cost to preserve hard isolation and upstream Agent Memory behavior without Enterprise licensing or pervasive query filtering.
