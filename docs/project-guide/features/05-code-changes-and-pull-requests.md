# 5. Code changes and pull requests

The agent carries out a plan in a sandbox, proves the fix with a test, has it reviewed, and opens a pull request. It never merges or deploys.

## How to use it

- `@agent implement the changes, commit and push them to a new branch, and raise a PR` in a thread with a plan
- the **Implement this plan** button on a plan
- after review comments on the pull request: `@agent address the review comments` in the same thread

## What happens

1. **Prepare**: a sandbox with the repository at the plan's commit. Your linked GitHub login must have write access. The sandbox has no internet access and no credentials.
2. **Implement**: the agent writes a test that reproduces the problem, then the smallest fix, using only sandbox tools (write, edit, run, show the diff).
3. **Verify**: the repository's tests run; then the rest of the change is set aside and the new test must **fail** without the fix. Two failed attempts stop the task, with what was tried and the end of the test output, and ask for the plan to be revised.
4. **Review**: a separate reviewer sees only the plan and the diff; one round of changes goes back to the implementer.
5. **Check the diff, in code**: secrets, CI workflow files, unsafe paths, binary files and links block the push outright. Large changes (over 20 files or 800 lines), protected paths, generated files, tests that could not run or a change without a test all need a person, even in Auto.
6. **Push**: in Ask mode the approval request shows the branch, the files and line counts, what was verified, what needs a person and which tickets will move. Whoever decides approves (you, an admin, or the role that approves pushes, such as a Tech Lead).
7. **Pull request**: a branch under `agent/`, a commit on the plan's base, and a pull request with the plan, what was verified and what was not. The answer links it with a **View pull request** button.

## Tickets follow the change

If the plan came from Jira tickets, they move as the work does: In Progress when the change starts (in Auto), In Review with a comment linking the pull request when it opens. Those moves were listed in the push's approval, so one decision covers them.

## Review comments and failed checks

Reviews, failed CI checks and merges on the pull request come back to the Slack thread as notices (read within your access). Nothing starts on its own: ask in the thread, and the agent revises the same pull request on its branch, addressing each comment, with the same verification and checks.

## Repository settings

Admins set per repository (console, Repositories page): protected paths (changes there always need a person) and the test command. Without a command the agent detects one (pytest, npm test, go test, cargo test) and the page shows how the tests last ran.
