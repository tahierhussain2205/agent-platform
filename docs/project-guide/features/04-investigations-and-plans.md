# 4. Investigations and plans

Ask the agent to investigate a problem and it reads the code, the recent changes and the logs, then writes a plan you can check claim by claim.

## How to use it

- `@agent investigate PAY-101 and plan a fix`
- `@agent come up with a detailed plan to address these tickets` (in a thread where tickets were created)
- the **Plan a fix** button on a tickets answer
- follow ups in the same thread: `it only happens for carts with discounts` revises the plan instead of starting over

## What happens

1. **The case file, found in code**: the issues (those you named, else the thread's current plan's, else the tickets the thread created; up to three), the service they belong to (from the catalog), the repository and log source, and a time window (from your request, else around when the issue was created).
2. **Three investigators in parallel**, each with only its own tools and its own context:

   | Investigator | Reads |
   |---|---|
   | Code | The repository in a sandbox at a pinned commit: searches, reads line ranges. An incident that ended a while ago is read in the code of its time |
   | Recent changes | Commits around the window with the files they touched, a commit's diff, deployments |
   | Logs | The service's log source: events grouped by error signature, with counts and samples |

3. **The plan**: a goal, evidence (each item citing the findings it comes from), ranked hypotheses (with confidence, the evidence they rest on and how to confirm them), steps, the files the work touches, risks, verification, rollback and open questions.
4. **Rules checked in code** before you see it:

   | Rule | Effect |
   |---|---|
   | Evidence must cite findings | Uncited items are dropped and listed under what was not verified |
   | A hypothesis needs evidence | Without it, it is low confidence and confirming it becomes the first step |
   | Bugs start with a failing test | The steps begin by reproducing the problem |
   | "High" confidence needs two kinds of evidence | For example code and recent changes, or code and logs |
   | Files nobody read are flagged | So the plan does not pretend to know them |
   | The plan's confidence is capped | Never above its leading hypothesis |

5. **The answer**: the plan titled with the tickets and what the change achieves ("Plan for KAN-15, KAN-16: Count every cart line and refuse discounts over 100% · confidence high"), sources as links to the exact lines, what could not be covered (for example no log source in the catalog), and an **Implement this plan** button.

The plan is stored as the thread's current plan. Implementing it later, or revising it, starts from it.

## What it never does

Investigations only read: no ticket changes, no pushes. Repository secrets (`.env` files, keys, credentials) are never read, searched or shown. Code is read through the requester's access: their linked GitHub login must be able to read the repository.
