---
key: implement_change
version: 1
title: Implement the plan and open a pull request
description: Carries out the thread's current plan in its repository and raises a pull request ("implement the plan", "go ahead and fix it", "raise a PR"), or revises the thread's open pull request ("address the review comments", "fix CI"). Tests first, verified in a sandbox, reviewed, checked, then pushed to a branch under agent/. Never merges.
read: github.read_code, github.search_code, github.list_files, github.read_file, github.get_pull_request, sandbox.*
write: github.push_changes
---
# Implement the plan and open a pull request

## What the change contains
- A test that reproduces the problem (it fails without the fix), then the smallest fix for the cause the plan names.
- Nothing unrelated: no reformatting, no new dependencies, no changes to CI workflows or secrets.

## How it is verified
- The repository's tests run in the sandbox; the new test must fail without the fix and pass with it.
- A reviewer who sees only the plan and the diff approves it or asks for one round of changes.
- When verification fails twice, stop and say what was tried; the plan needs revising before another attempt.

## What leaves the sandbox
- Only the diff. It is checked in code (size, protected paths, workflows, secrets, binaries, unsafe paths) and pushed by the platform to a branch under agent/, never to the default branch and never forced.

## In the thread
- Report the pull request, the files changed, the checks, and what was and was not verified.
- Review comments and failed checks on the pull request come back to the thread; "address the review comments" revises the same pull request.
