# Use async Provisioning Jobs and an Operator Service

Admin-driven Tenant creation will use Control Store Provisioning Jobs requested through `/api/v1/admin/*` and executed by a separate Operator Service that polls, claims, heartbeats, and updates those jobs. This preserves the existing decision that the gateway does not receive Docker or host mutation authority, keeps provisioning resumable and auditable, and permits explicit never-active Provisioning Cleanup without introducing automatic destructive rollback.

