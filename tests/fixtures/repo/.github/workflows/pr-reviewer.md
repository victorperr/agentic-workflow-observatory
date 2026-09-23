---
on:
  pull_request:
    types: [opened, synchronize]
permissions:
  contents: read
  pull-requests: read
engine: claude
max-ai-credits: 100
safe-outputs:
  add-comment:
    max: 1
---

# PR Reviewer

Review the pull request diff. Post one comment that lists concrete bugs or risky changes,
each with the file and line. If the diff is only docs or formatting, call `noop` with a reason.
