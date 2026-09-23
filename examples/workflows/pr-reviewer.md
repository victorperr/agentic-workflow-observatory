---
description: Review pull requests and post one comment with concrete findings.
on:
  pull_request:
    types: [opened, synchronize]
permissions:
  contents: read
  pull-requests: read
engine: claude
max-turns: 20
max-ai-credits: 300
network:
  allowed:
    - defaults
    - node
tools:
  github:
    toolsets: [pull_requests, repos]
safe-outputs:
  add-comment:
    max: 1
  noop:
---

# PR Reviewer

Review the diff of pull request #${{ github.event.pull_request.number }}.

Post **one** comment that lists concrete bugs, security issues, or risky changes. For each one,
give the file path and line and explain the impact in one sentence. Do not restate the diff.

If the change is only documentation, formatting, or dependency lock files, call `noop` with a
one-line reason instead of commenting.
