PYTHON ?= python3.12

.PHONY: config down format-check integration lint logs package ps public-tree setup test typecheck up verify

setup:
	@PYTHON=$(PYTHON) sh scripts/setup-dev-secrets.sh

public-tree:
	$(PYTHON) scripts/verify-public-tree.py

format-check:
	$(PYTHON) -m ruff format --check .

lint:
	$(PYTHON) -m ruff check .

typecheck:
	$(PYTHON) -m mypy --no-incremental src tests

test:
	$(PYTHON) -m pytest -q --ignore=tests/integration

integration:
	@sh scripts/verify-infrastructure.sh

package:
	$(PYTHON) -m build
	$(PYTHON) -m twine check dist/*
	$(PYTHON) scripts/verify-package.py

config: setup
	TENANT_SECRETS_GID=$$(id -g) docker compose config --quiet
	docker compose -f deploy/shared.compose.yaml --env-file deploy/shared.env.example config --quiet
	TENANT_ID=tenant-a TENANT_TELEMETRY_REF=o_configcheck TENANT_SECRETS_DIR=./secrets/tenants docker compose -f deploy/tenant.compose.yaml config --quiet

verify:
	@$(PYTHON) scripts/verify-release.py

up: setup
	TENANT_SECRETS_GID=$$(id -g) docker compose up --detach --build

ps:
	TENANT_SECRETS_GID=$$(id -g) docker compose ps

logs:
	TENANT_SECRETS_GID=$$(id -g) docker compose logs --follow

down:
	TENANT_SECRETS_GID=$$(id -g) docker compose down
