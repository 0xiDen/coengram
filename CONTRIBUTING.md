# Contributing to CoEngram

CoEngram welcomes focused fixes, documentation improvements, tests, and design
proposals that preserve its privacy and Tenant-isolation guarantees.

## Before opening a change

1. Search existing issues and the iteration-1 PRD for the relevant decision.
2. Open an issue before making a large architectural change.
3. Keep credentials, deployment hostnames, memory content, prompts, tokens, and private
   Tenant identifiers out of commits, fixtures, logs, screenshots, and issue text.

## Development setup

```sh
git clone https://github.com/0xiDen/coengram.git
cd coengram
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
make setup
make config
```

Run the fast host-side checks while developing:

```sh
make format-check
make lint
make typecheck
make test
```

Before opening a pull request, run the complete provider-neutral gate:

```sh
make verify
make package
```

The integration gate builds the real application and Caddy images, then uses temporary
PostgreSQL, RabbitMQ, and isolated Neo4j Community containers. It can take several
minutes on the first run.

## Design constraints

- Identity and Tenant context are derived from authenticated server state, never from
  caller-selected storage or owner arguments.
- Private Memory and Tenant Knowledge are different scopes joined only through human
  review and durable publication.
- PostgreSQL is authoritative for commands and workflow state; Neo4j is the recall
  projection; RabbitMQ transports committed outbox events.
- Every state-changing operation is idempotent and has a failure/recovery contract.
- Telemetry must be useful without containing memory, prompts, credentials, tokens,
  provider payloads, Telegram text, or raw Tenant identifiers.
- Neo4j Community remains one instance per Tenant unless an explicit architecture
  decision changes the isolation model.

Read [CONTEXT.md](CONTEXT.md), [docs/architecture.md](docs/architecture.md), and the
[ADRs](docs/adr/) before changing a boundary.

## Pull requests

Keep pull requests small enough to review as one coherent change. Include:

- the behavior and security boundary being changed;
- tests that fail without the change and pass with it;
- migration, recovery, and rollback implications;
- documentation updates for any public contract or operator step;
- confirmation that `make verify` and `make package` pass.

Do not commit generated secrets, `.env` files, local model data, build output, or real
deployment hostnames.

By contributing, you agree that your contribution is licensed under the repository's
[MIT License](LICENSE).
