# Contributing

Thanks for contributing to Agentic Workflow Observatory.

## Development

```powershell
py -m pip install -e .
py -m pytest
py -m compileall -q src tests
```

Keep changes focused, add tests for behavior changes, and do not commit Datadog keys, workflow tokens, Terraform state, or customer telemetry.

## Pull requests

Describe the problem, the telemetry contract affected, and how you validated the change. Changes to event kinds or metric names require an update to `schemas/agentic-event.schema.json` and the Terraform documentation.
