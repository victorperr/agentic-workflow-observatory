# Agentic Workflow Observatory

**Datadog Agent Observability for [GitHub Agentic Workflows](https://github.com/github/gh-aw).**
Every agentic workflow run becomes a Datadog trace that knows which repository, pull request, commit and
Markdown workflow it came from. Runs that pass CI but still failed as agents get flagged.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

> ⚠️ Experimental / Work in Progress: This project is currently experimental and may contain bugs or breaking changes. Use with caution.

## Prerequesites

- **Datadog account** 🐶 with Agent Observability enabled
- A repository already running **GitHub Agentic Workflows**

## 🎯 About The Project

A GitHub agentic workflow run does not fail the way a unit test does. A run can finish with a green check and still:

- post a **useless PR comment**,
- **loop**, calling the same tool with the same arguments five times when one call would do,
- have its egress **blocked by the AWF firewall**, so `npm install` fails and the agent works around it without saying so,
- run out of its **AI Credits budget** (`max-ai-credits`) or `max-turns` partway through its reasoning.

Datadog Agent Observability does capture reasoning, tool calls and cost, but it knows nothing about GitHub.
It cannot tell that a trace belongs to *run 9876543210 of `pr-reviewer.md` on PR #42*.
This project connects the two.

## 🔎 What it does

```mermaid
flowchart LR
    A["Your Agentic workflow run"] -- "completed" --> B["workflow_run trigger/ companion workflow trigger"]
    B -- "gh run download" --> C["gh-aw artifacts - aw_info -agent-stdio.log-token-usage.jsonl-squid access.log-agent_output.json"]
    C --> D["Collector parse → correlate → detect"]
    D -- "spans + session" --> E["Datadog LLM Observability"]
    D -- "evaluations" --> E
    D -- "agentic.run.* metrics" --> F["Datadog dashboard + monitors"]
```

After each agentic run finishes, a [small companion workflow](examples/workflows/agentic-observatory.yml) downloads the run's artifacts. gh-aw already uploads
them, so **your agentic workflows need no changes**. The collector then:

1. **Correlates.** It reads the `workflow_run` event to get the repository, PR, SHA, CI conclusion and the compiled lock file. From the lock file it finds the Markdown source (`foo.lock.yml` → `foo.md`).
2. **Rebuilds the trace.** It turns the artifacts into one LLM Observability trace per run:
   - an `agent` root span (input: the workflow's Markdown instructions; output: what the agent posted),
   - an `llm` child span per model call (tokens, AI Credits),
   - a `tool` child span per tool call (errors marked),
   - a firewall span and one span per safe output.

   Every run on the same PR shares one **session** (`owner/repo#pr-42`), so you can replay an agent's history on a PR.
3. **Detects silent failures** with deterministic detectors (see below).
4. **Ships** spans, evaluations and low-cardinality metrics to Datadog over its public HTTP intake APIs. No Datadog Agent is needed on the runner.

Each run gets one of four verdicts:

| Verdict | Meaning |
|---|---|
| `healthy` | CI green, no findings |
| `degraded` | CI green, warnings only |
| `silent_failure` | **CI green, but the agent failed.** This is the case nothing else catches. |
| `failed` | CI red (already visible in GitHub) |

## Quick start 

**1. Add the Datadog API key** as a repository or organization secret named `DD_API_KEY`.

**2. Drop in one workflow.** Copy [`examples/workflows/agentic-observatory.yml`](examples/workflows/agentic-observatory.yml)
to `.github/workflows/` in your repo and list the agentic workflows to observe:

```yaml
on:
  workflow_run:
    workflows: ["PR Reviewer", "Issue Triage"] # List here
    types: [completed]
permissions:
  actions: read
  contents: read
jobs:
  observe:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { sparse-checkout: .github/workflows }
      - uses: victorperr/agentic-workflow-observatory@v1
        with:
          datadog-api-key: ${{ secrets.DD_API_KEY }}
```
Now you can go on Agent Observability and check the traces.

**3. (Optional) Deploy the dashboard and monitors** once per Datadog org:

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # add API + app keys, notification handles
terraform init && terraform apply
```

The Python collector sends agentic.run.* metrics to Datadog after each run. On their own, those are just raw numbers.The Terraform files create a **dashboard** (runs by verdict, AI Credits by workflow, findings, budget utilization, tool calls) and four monitors: 
| Monitor | Fires when |
|---|---|
| `silent_failures` | At least one run in the last hour was green in CI but actually failed |
| `firewall_blocks` | The agent tried to reach a domain it isn't allowed to reach |
| `budget` | A workflow uses more than 80% of its AI Credits budget on average |
| `tool_loops` | At least 2 runs in 4 hours got stuck repeating the same tool call |



### Action inputs

| Input | Default | |
|---|---|---|
| `datadog-api-key` | *(required)* | |
| `datadog-site` | `datadoghq.com` | `datadoghq.eu`, `us3.datadoghq.com`, `us5.datadoghq.com`, `ap1.datadoghq.com` |
| `ml-app` | `github-agentic-workflows` | LLM Observability application name |
| `budget-aic` | `1000` | Set to the workflow's `max-ai-credits` |
| `loop-threshold` | `3` | Identical tool calls before `tool_loop` fires |
| `max-tool-calls` | `40` | |
| `fail-on` | `never` | `silent_failure` or `degraded` makes the observer job fail, so the problem shows up in the checks UI |
| `dry-run` | `false` | Print the payloads instead of sending them |

Outputs: `verdict`, `trace-id`.

If needed, every input in the "Action inputs" table goes in the with: block of that step.
```yaml
      - uses: victorperr/agentic-workflow-observatory@v1
        with:
          datadog-api-key: ${{ secrets.DD_API_KEY }}   # required
          datadog-site: datadoghq.eu
          ml-app: github-agentic-workflows
          budget-aic: "300"
          loop-threshold: "3"
          # ...
```


## Detectors

### Findings

| Code | Severity | Signal | Source artifact |
|---|---|---|---|
| `tool_loop` | error | Same tool + same arguments ≥ N times | `agent-stdio.log` / MCP gateway logs |
| `excessive_tool_calls` | warning | More tool calls than the threshold | same |
| `tool_errors` | warning | At least 1/3 of tool calls returned errors | `tool_result.is_error` |
| `firewall_blocked` | error | Egress denied by the AWF firewall (names the domains to add to `network.allowed`) | Squid `access.log` |
| `budget_exhausted` / `budget_near_limit` | error / warning | AI Credits ≥ 100% / ≥ 80% of budget, or the proxy stopped the agent | `token-usage.jsonl`, stdio |
| `max_turns_reached`, `agent_timeout` | error | Agent was cut off | `agent-stdio.log` |
| `no_output` | error | Green run with no safe output and no explicit `noop` | `agent_output.json` |
| `safe_output_invalid` | error | Safe output rejected by validation | `agent_output.json` |
| `low_quality_output` | warning | Too short, unrendered `${{ }}`, TODO/placeholder, "as an AI model", repeated paragraphs, agent admitting it could not do the task | safe output body |
| `missing_tool`, `missing_data`, `gave_up` | warning | Agent reported it lacked a tool or data | safe outputs |
| `threat_detected` | error | gh-aw threat detection flagged the run | `detection_result.json` |

### Custom Evaluations

We [send](src/aw_observatory/datadog.py#L199-L229) our own custom evaluations : detector results are sent as Agent Observability **evaluations** attached to the root span 

**How they're calculated:** no LLM is involved. Each value is a simple rule applied to the detector results (the `findings`). Every evaluation also gets an `assessment` of pass or fail:

| Evaluation | Type | Value | Pass when |
|---|---|---|---|
| `aw_verdict` | categorical | `failed` if CI failed; otherwise `silent_failure` if any **error** finding; otherwise `degraded` if any **warning**; otherwise `healthy` ([detectors.py:168-176](src/aw_observatory/detectors.py#L168-L176)) | verdict is `healthy` or `degraded` |
| `aw_tool_efficiency` | score 0–1 | unique (tool, arguments) pairs ÷ total tool calls. 10 calls with only 4 distinct ones gives **0.4**. It's 1.0 if there were no calls | no `tool_loop` finding |
| `aw_firewall_clean` | boolean | `true` if there's no `firewall_blocked` finding | same |
| `aw_budget_utilization` | score | AI Credits spent ÷ budget (1000 by default) ([model.py:137-138](src/aw_observatory/model.py#L137-L138)) | no `budget_exhausted` / `budget_near_limit` finding |
| `aw_output_quality` | boolean | `true` if none of `no_output`, `low_quality_output`, `safe_output_invalid` fired | same |

`aw_verdict` and `aw_tool_efficiency` also carry a `reasoning` text: the finding messages for the verdict, and "4 unique of 10 tool calls" for efficiency.

You can filter and chart them next to the traces, and they can feed Datadog's annotation queues and experiments.


## What you see in Datadog

- **Agent Observability → Traces**: filter by `gh.repo:acme/webapp`, `gh.pr:42`, `aw.verdict:silent_failure` or `aw.finding:firewall_blocked`. The root span's metadata links back to the GitHub run.
- **Sessions**: every agent run on a PR, in order.
- **Metrics** (`agentic.run.*`): `count`, `silent_failure`, `aic`, `budget_utilization`, `tokens.{input,output,cache_read}`, `tool_calls`, `firewall.blocked_requests`, `duration_seconds`, `finding{finding:<code>}`.
  Metric tags are deliberately low-cardinality (`repo`, `workflow`, `engine`, `model`, `verdict`). Run IDs and PR numbers are only on traces, so custom-metric costs stay bounded.
- **GitHub job summary**: a verdict, a usage table, the findings, and a deep link to the trace.

## Try it locally

No Datadog account is needed for a dry run. The repo ships two sample runs in [`tests/fixtures/runs`](tests/fixtures/runs):

```bash
python -m pip install -e ".[dev]"
aw-observatory collect \
  --run-dir tests/fixtures/runs/silent-failure \
  --event tests/fixtures/runs/silent-failure/event.json \
  --repo-root tests/fixtures/repo \
  --budget-aic 100 --dry-run --summary summary.md
```

That run exits green in CI, but the collector reports:
- `tool_loop` (the diff tool was called 4 times with identical arguments),
- `tool_errors`,
- `firewall_blocked` on `registry.npmjs.org`,
- `budget_near_limit` (88.5 of 100 AIC),
- `low_quality_output` (a 32-character "LGTM … TODO" comment).

Verdict: **silent_failure**.

To backfill real runs, download them with `gh run download <run-id> -D run/` (or `gh aw logs`), then run
`aw-observatory collect --run-dir run/ --repo owner/name` with `DD_API_KEY` set.

## Repository layout

```
action.yml                      Composite GitHub Action (what customers install)
src/aw_observatory/
  context.py                    workflow_run event → RunContext (repo, PR, SHA, .md source)
  artifacts.py                  gh-aw artifacts → RunTelemetry (tolerant to layout changes)
  detectors.py                  silent-failure detectors and verdict
  datadog.py                    LLM Obs spans / evaluations / metrics payloads + HTTP client
  pricing.py                    AIC estimate when the proxy did not report it
  report.py, cli.py             JSON record, job summary, CLI
schemas/agentic-event.schema.json   normalized run record (`--output`)
terraform/                      Datadog dashboard + monitors
examples/workflows/             drop-in observer workflow + sample gh-aw workflow
tests/                          pytest suite with realistic run fixtures
```


## Limitations

- gh-aw's token log does not include prompt or completion text, so `llm` spans carry token counts and cost but no messages.
- When artifacts have no per-call timestamps (tool call,...), child spans are laid out evenly across the run window. The order and the count are right, but the exact positions and lengths of the bars might not be real.
- When the API proxy does not report AI Credits, cost is estimated from list prices in [`pricing.py`](src/aw_observatory/pricing.py) and marked with `*` in the summary.
- The `workflow_run` event only lists PRs from the same repository, so runs triggered from forks are grouped by branch instead of by PR.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

## License

[MIT](LICENSE)
