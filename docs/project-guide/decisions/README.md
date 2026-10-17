# Decisions

The key technical decisions behind the platform, why we made them, what they cost, and what we turned down.

## How decisions were made and recorded

- The specification (`spec/slack_agent_platform_spec.md`, section 5) settled the product, technical and data model decisions before any code: each reviewed, with the main reason for rejected alternatives.
- During the build, every decision with a lasting effect was logged in `spec/decisions_and_blockers.md` (D-001 to D-205), with its reason. Blockers (missing accounts, unknown platform behavior) were logged there too, with the workaround used until they were cleared.
- The invariants in `CLAUDE.md` are the decisions that are never traded away: a change that would weaken one stops and is raised instead.

This folder distills the decisions a reviewer most needs, grouped by theme. Each one says **what** we decided, **why**, the **tradeoffs** we accepted and the **alternatives** we considered, with references to the full log (D-numbers) or the specification (T-numbers).

## The decisions

| Theme | Decisions |
|---|---|
| [1. Platform and infrastructure](01-platform-and-infrastructure.md) | Temporal for durable execution · Postgres as the source of truth · one image, many commands · managed services behind our own interfaces · stand-ins for every external service · Render for staging |
| [2. Security and multi tenancy](02-security-and-tenancy.md) | Safety in layers · row level security with a unit of work · a login per component · the policy module in code · act within the requester's access · a separate vault service · no raw Slack text stored · humans approve what cannot be undone |
| [3. Agent design](03-agent-design.md) | Our own thin orchestrator · one loop, many playbooks · isolated subagents with minimal tools · structured output checked in code · numbers and citations by code · one model family, tiers per step · a stand-in model for tests |
| [4. Code changes](04-code-changes.md) | The coding loop outside the sandbox · only the diff leaves · a trusted pusher · reproduce first and an independent review · tickets that follow the change · E2B for staging |
| [5. Product and experience](05-product-and-experience.md) | Slack first, console for admins · one status message · Ask and Auto with ceilings · shadow mode · roles that only narrow · buttons that carry intents · a Tasks page from existing records |
| [6. Delivery and process](06-delivery-and-process.md) | A phased plan with written plans and logs · testing depth over breadth · blockers as workarounds, not waits · demo runs on staging · CI paused for the hackathon |

## The decision that shapes the rest

![Safety in layers](images/safety-layers.png)

The model is the least trustworthy component: it can be persuaded by anything it reads. So it **proposes** and everything after it **decides**: checks in code, the policy module, people, the database and isolation. Most decisions in this folder follow from that.
