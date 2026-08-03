# Use tenant-scoped access tokens before SSO

The first production version authenticates every User and Agent with an individually revocable opaque Access Token bound to one Tenant Membership. This gives Claude and service agents a uniform Bearer-token flow with server-derived identity while avoiding an identity-provider deployment now; authentication remains behind a narrow boundary so OIDC/SSO can replace token issuance later without changing memory scoping.
