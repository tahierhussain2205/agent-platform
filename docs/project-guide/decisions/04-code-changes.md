# 4. Code changes

## The coding loop outside the sandbox

**Decision.** The implementer is a subagent of our own loop, running in the worker; its sandbox tools act through the provider's API. No model key, proxy or credential exists inside the sandbox, so the sandbox's network can be turned off entirely. (D-168.)

**Why.** Strongest isolation available: nothing in the sandbox can call out, so nothing in the repository (a malicious test, a post install script) can exfiltrate anything. One loop and one policy path for every tool call, including edits.

**Tradeoffs.** We gave up running a mature coding agent (the Claude Agent SDK) inside the sandbox, which the specification first planned. That needs egress to a model proxy, and is kept as a later option.

## Only the diff leaves

**Decision.** The platform fetches the repository archive with a read only token and uploads it; a baseline commit is made of exactly what was uploaded; the change leaves as the staged diff against it (patch plus file contents) and is stored as an artifact. The sandbox is destroyed afterwards. (D-170, D-176; invariant 6.)

**Why.** The sandbox never holds a credential, and what leaves is small, exact and reviewable. Approval requests show precisely what will be pushed, and the pusher pushes exactly that.

**Tradeoffs.** Large repositories mean large archives; changes to binary files are not supported (they block the push).

## A trusted pusher

**Decision.** Only `github.push_changes`, executed through the agent's idempotent action path, pushes. It re-runs the blocking checks, commits on the prepared base through the Git Data API with an idempotency trailer, pushes only to branches under `agent/`, never the default branch, never forced, and opens the pull request. The agent cannot merge or deploy. (D-177, D-179; invariants 8 and 10.)

**Why.** One small, well tested piece of code holds the write path to repositories. The trailer makes a retry after a lost answer find its own commit instead of pushing twice. Branch rules keep the agent's work clearly separate and reviewable.

**Tradeoffs.** A person must merge every pull request. That is the point.

## Reproduce first, then an independent review

**Decision.** Verification is code: the tests run, and when the change adds a test, the rest of the change is set aside and the new test must fail. Two failed verifications stop the task. A reviewer subagent sees only the plan and the diff, with one round of changes. Diff checks block secrets, workflows, unsafe paths and binaries, and send large, protected, generated or untested changes to a person even in Auto. (D-173, D-174, D-175.)

**Why.** "The tests pass" proves little if the new test would have passed anyway. A test that fails without the fix shows the change addresses the reported problem. Stopping after two failures avoids an agent flailing at a wrong plan.

**Tradeoffs.** Changes that cannot be tested in the sandbox (no test runner, missing dependencies without internet) always need a person, and docstring only changes add no test, so they ask too.

## Tickets follow the change

**Decision.** When a plan came from Jira tickets, the change moves them: In Progress when it starts (only where the policy allows it unasked, i.e. in Auto), In Review with a link to the pull request when it opens. The moves are listed in the push's approval request and ride on that decision. (D-198.)

**Why.** The board should reflect the work without people updating it. Asking for approval of each status change would be clicks without decisions; the push approval is the real decision, so the moves are part of it.

**Tradeoffs.** In Ask mode tickets jump from To Do straight to In Review. Moving to Done on merge was left out, because webhooks never start work on their own.

## E2B for staging

**Decision.** Staging sandboxes run on E2B: a Firecracker microVM per sandbox from our template, internet access off, at most an hour. The client keeps the local stand-in's contract exactly, so nothing above the interface changed. (D-197.)

**Why.** The local runner cannot be deployed safely (Render cannot cut a container's network, and every task would share one container). E2B is built for agent sandboxes, has a free plan, and maps onto our interface almost one to one.

**Tradeoffs.** A vendor dependency with plan limits. One surprise found while checking it: with the internet off, E2B's firewall still accepts a TCP connection and then closes it, so our network check sends a request and expects no answer rather than trusting a refused connection.
