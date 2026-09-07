# Add a role-based Operator admin plane

CoEngram will add Operators as deployment-wide administrative identities separate from Tenant Principals, with Operator Access Tokens stored as non-reversible Control Store verifiers and browser Admin Sessions created after token authentication. Operator Roles grant bounded admin powers without creating Tenant Membership or Private Memory access; slice 1 keeps current token expiry defaults and 30-day unused-token warnings, while later policy work may add Tenant-configurable expiry presets, unused-token thresholds, and Operator Audit Event retention.
