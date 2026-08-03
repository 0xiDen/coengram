# Use file-backed Compose secrets in iteration 1

Production credentials are generated or supplied as protected, gitignored host files and mounted through Docker Compose secrets, while `.env` contains only non-secret settings. This keeps secrets out of manifests and source control without adding a secret-management platform now; rotation remains explicit and the secret-loading seam can later adopt Vault or SOPS.
