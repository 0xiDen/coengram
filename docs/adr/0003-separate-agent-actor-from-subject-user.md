# Separate the Agent Actor from the Subject User

A delegated Agent authenticates as itself while a server-side Delegation identifies the Subject User whose Private Memory it may access. Each delegated Access Token binds one Agent, one Tenant, and one Subject User; keeping those identities distinct prevents impersonation and confused-deputy access, preserves attribution, and lets autonomous Agents retain their own Private Memory without gaining access to any User's private scope.
