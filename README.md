# Slack Agent Platform

A multi tenant AI agent that works as a teammate inside Slack: it summarizes conversations, files Jira tickets, investigates bugs in code, logs and recent changes, writes plans, changes code in a sandbox and opens pull requests, always within the requesting person's own access. Admins configure it in a web console.

**Start here: [the project guide](docs/project-guide/README.md)**, with the [technical documentation](docs/project-guide/technical/README.md), the [features](docs/project-guide/features/README.md) and the [key decisions](docs/project-guide/decisions/README.md), illustrated with diagrams.

## Run it locally

```bash
make install   # backend (uv) and console (pnpm) dependencies
make dev       # the whole stack in Docker Compose, with stand-ins for every external service
```

Then open the console at http://localhost:5173 (seeded account `alice@acme.test`, password `password123`) and the Slack stand-in at http://localhost:4100/slack/ui.

## More

| | |
|---|---|
| Requirements and design | [spec/slack_agent_platform_spec.md](spec/slack_agent_platform_spec.md) |
| Phased plan and per phase plans | [spec/implementation_plan.md](spec/implementation_plan.md), [docs/plans/](docs/plans/) |
| What each phase delivered | [docs/progress.md](docs/progress.md) |
| Decisions and blockers log | [spec/decisions_and_blockers.md](spec/decisions_and_blockers.md) |
| Demo runs on staging | [docs/demo_test_log.md](docs/demo_test_log.md) |
| Working rules for contributors | [CLAUDE.md](CLAUDE.md) |
