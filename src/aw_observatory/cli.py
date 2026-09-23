"""Command line entry point: `aw-observatory collect --run-dir <artifacts> [--event <workflow_run.json>]`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from aw_observatory import artifacts, context
from aw_observatory.datadog import DatadogClient, build_evaluations, build_metrics, build_spans
from aw_observatory.detectors import Thresholds
from aw_observatory.report import build_report, to_markdown, to_record

log = logging.getLogger("aw_observatory")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aw-observatory", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect", help="analyze one agentic workflow run and ship it to Datadog")
    c.add_argument("--run-dir", required=True, help="directory with the run's downloaded artifacts")
    c.add_argument("--event", default=os.getenv("GITHUB_EVENT_PATH"),
                   help="workflow_run event JSON (defaults to $GITHUB_EVENT_PATH)")
    c.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY"), help="owner/name, when no event is given")
    c.add_argument("--repo-root", default=".", help="checkout used to read the workflow's Markdown source")
    c.add_argument("--ml-app", default=os.getenv("DD_LLMOBS_ML_APP", "github-agentic-workflows"))
    c.add_argument("--site", default=os.getenv("DD_SITE", "datadoghq.com"))
    c.add_argument("--budget-aic", type=float, default=1000.0, help="the workflow's max-ai-credits")
    c.add_argument("--loop-threshold", type=int, default=3)
    c.add_argument("--max-tool-calls", type=int, default=40)
    c.add_argument("--dry-run", action="store_true", help="print Datadog payloads instead of sending them")
    c.add_argument("--output", help="write the normalized JSON record here")
    c.add_argument("--summary", default=os.getenv("GITHUB_STEP_SUMMARY"), help="append a Markdown summary here")
    c.add_argument("--fail-on", choices=["never", "silent_failure", "degraded"], default="never",
                   help="exit non-zero when the verdict is at least this bad")
    return p


def collect(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    aw_info = artifacts.load_aw_info(run_dir)
    ctx = context.load_context(args.event, aw_info, args.repo)
    tel = artifacts.load_run(run_dir, Path(args.repo_root), ctx.source_path)
    log.info("run %s/%s: %d llm calls, %d tool calls, %d firewall entries, %d outputs",
             ctx.repository, ctx.run_id, len(tel.llm_calls), len(tel.tool_calls), len(tel.firewall),
             len(tel.safe_outputs))

    thresholds = Thresholds(budget_aic=args.budget_aic, loop_threshold=args.loop_threshold,
                            max_tool_calls=args.max_tool_calls)
    report = build_report(ctx, tel, thresholds)
    record = to_record(report)

    if args.output:
        Path(args.output).write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    markdown = to_markdown(report, args.site, args.ml_app)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(markdown)
    if gh_output := os.getenv("GITHUB_OUTPUT"):
        with open(gh_output, "a", encoding="utf-8") as fh:
            fh.write(f"verdict={report.verdict}\ntrace-id={record['trace_id']}\n")

    if args.dry_run:
        payloads = {"spans": build_spans(report, args.ml_app),
                    "evaluations": build_evaluations(report, args.ml_app),
                    "metrics": build_metrics(report)}
        print(json.dumps(payloads, indent=2, default=str))
        if not args.summary:
            # Legacy Windows consoles cannot encode the verdict emoji.
            sys.stderr.write(markdown.encode(sys.stderr.encoding or "utf-8", "replace").decode(
                sys.stderr.encoding or "utf-8"))
    else:
        api_key = os.getenv("DD_API_KEY")
        if not api_key:
            log.error("DD_API_KEY is not set (use --dry-run to test without Datadog)")
            return 2
        DatadogClient(api_key=api_key, site=args.site).send(report, args.ml_app)
        log.info("sent trace %s to Datadog (%s): verdict=%s", record["trace_id"], args.site, report.verdict)

    order = ["healthy", "degraded", "silent_failure", "failed"]
    if args.fail_on != "never" and order.index(report.verdict) >= order.index(args.fail_on):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(argv)
    return collect(args)
