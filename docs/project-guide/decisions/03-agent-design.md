# 3. Agent design

## Our own thin orchestrator

**Decision.** The agent loop is our own code (`AgentRunner` and the task workflow), not an agent framework or an SDK's loop. (Spec T7, D-168.)

**Why.** Policy checks, tenancy, credentials, Slack delivery, budgets, approvals and durability all need to be woven into the loop at specific points. A framework would either hide those points or need to be worked around. Our loop is small, and every step is a Temporal activity we can test and replay.

**Tradeoffs.** We maintain the loop, the tool calling and the context handling ourselves, and do not get a framework's ecosystem for free.

**Alternatives.** An agent framework (LangGraph and similar); the Claude Agent SDK inside the sandbox for coding (the specification's first direction, kept as a later option, see [code changes](04-code-changes.md#the-coding-loop-outside-the-sandbox)).

## One loop, many playbooks

**Decision.** A single generic loop (understand, gather, plan, act, verify, report) runs versioned playbooks: Markdown files with the instructions and the operations they may read and write with. Built in versions are immutable; tasks pin the version they started with. (Spec 3.5, 7.1.)

**Why.** New use cases come from playbooks, tools and roles rather than new code paths. Pinning makes a deploy safe for running tasks, and immutable versions make behavior reproducible.

**Tradeoffs.** Some work does need its own path: investigations (parallel investigators) and code changes (a multi step pipeline with a sandbox) have dedicated steps in code, still driven by their playbooks.

## Isolated subagents with minimal tools

**Decision.** Reading is done by subagents with their own context, offered only the read tools the playbook allows. Investigations run three investigators (logs, code, changes) in parallel, each with only its own tools. The code reviewer and the plan writer get no tools at all. (Spec 7.4, D-087, D-162, D-174.)

**Why.** Smaller contexts give better results and cost less; parallel investigators finish sooner; and a model with fewer tools has fewer ways to be misled. An independent reviewer that sees only the plan and the diff is not swayed by the implementer's reasoning.

**Tradeoffs.** Findings have to be summarized well enough to hand over, and a subagent cannot ask the main context for clarification.

## Structured output, checked in code

**Decision.** Every step submits its result through a forced `submit_*` tool with a JSON schema. The gateway repairs tool inputs against the schema (arrays written as strings, stray call markup); code then validates what was proposed before using it. (D-187, D-194.)

**Why.** Free text is hard to check; a schema gives code something definite to validate. Repair exists because real models occasionally return a field as a string or leave markup at the end of a string, which once made a plan look empty and an answer claim a ticket that was never created. Fixing it in the gateway covers every step.

**Tradeoffs.** Models that reject a forced tool choice are asked with an instruction instead, with code still checking the call happened. The schemas are part of the contract with the stand-in model, so changing a tool name means updating the stand-in.

## Numbers and citations by code

**Decision.** Report figures are computed by code; the model writes only the narrative, which may not state a number that is not among the figures. Answers cite refs that code maps to real sources; citations are verified, and what could not be read is reported in the tools' own words. Ticket, plan and diff rules live in code with reasons. (Spec 4.6.6, 7.3, D-163, D-175, D-188.)

**Why.** Models are good at prose and bad at arithmetic and at remembering exactly where a fact came from. Putting each job where it is reliable makes the output trustworthy.

**Tradeoffs.** More code per playbook, and occasional refusals or "not verified" notes where a looser design would have produced a confident answer.

## One model family, tiers per step

**Decision.** One provider family (Anthropic's Claude). Each step asks for a tier (fast for classification and briefs, standard and advanced for planning and coding); admins choose which tiers their org allows. All calls go through our gateway with budgets, rate limits and metering. (Spec T8, T12.)

**Why.** Quality and testing over breadth: one family's behavior to learn and evaluate. Tiers keep cheap steps cheap. Budgets checked before every call mean a task stops cleanly instead of overspending, and never in the middle of a write.

**Tradeoffs.** No per org choice of provider; a provider outage affects everyone (the gateway has a fallback route within the family).

## A stand-in model for tests

**Decision.** Tests and the local stack use a deterministic, heuristic stand-in for the Anthropic API that recognizes each step by its forced tool and answers like a careful model would; a gitignored override points the local worker at the real model, and staging uses it. (D-088, D-186.)

**Why.** End to end tests need stable answers and must not cost money or depend on a network. Scripted responses let tests play a persuaded model and prove that code, not the model, stops it.

**Tradeoffs.** The stand-in proves the pipeline works, not that a model reasons well. That is measured separately: evals against the real model, and demo runs on staging, which found and fixed issues the stand-in could not show (D-188, D-194).
