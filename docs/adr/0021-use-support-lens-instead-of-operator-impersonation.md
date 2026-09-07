# Use Support Lens instead of Operator impersonation

Operators may need to diagnose tenant-scoped state, but they should not become a User or receive a User's Private Memory privileges through the admin panel. CoEngram will model this as an audited Support Lens over explicit tenant metadata and governance records, with any future delegated user-session mode requiring a separate ADR because it changes the privacy boundary.
