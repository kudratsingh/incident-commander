#!/usr/bin/env python3
"""Chaos setup helper — put the live platform into a fixable state.

Live remediation scenarios (``remediate_*``) assume something is broken; against a healthy
platform the run is green for nothing. This seeds a real failure right before
``make eval-live``, and every effect self-cleans on TTL (5–10 min). The five hooks map to
Tier-1 remediations: kill_consumer → restart_consumer_group, poison_message →
replay_dlq_messages, saturate_redis → invalidate_cache_key, inject_latency → restart,
bad_deploy → get_deploy_history.

Usage (``--help`` lists all subcommands):

    export PLATFORM_MCP_URL=http://localhost:8001/mcp
    export PLATFORM_CHAOS_TOKEN=sa_...     # the evaluator's chaos:invoke token
    uv run python scripts/chaos_setup.py kill-consumer --group worker-dispatcher

``chaos:invoke`` is its own principal since v0.6.5 — ``incident-commander-chaos``, printed
as ``PLATFORM_CHAOS_TOKEN`` by ``make bootstrap-token``. The AGENT's ``PLATFORM_TOKEN`` does
NOT carry it and is refused here, because the platform withholds the ``chaos.%`` audit rows
from principals that cannot fire chaos (platform ADR 0012, O-4). On a 403 re-run
``make bootstrap-token``; never widen the agent account. Hooks need ``CHAOS_ENABLED=true``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Any

from evals.chaos_hooks import ChaosClient


def _print_result(action: str, result: dict[str, Any]) -> None:
    """Echo what the platform returned for one hook."""
    print(f"[{action}] platform returned:")
    print(json.dumps(result, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    """The CLI: one subcommand per chaos hook, plus the shared connection flags."""
    parser = argparse.ArgumentParser(
        prog="chaos_setup",
        description=("Fire platform chaos hooks to seed a fixable state for live eval."),
    )
    parser.add_argument(
        "--mcp-url",
        default=os.environ.get("PLATFORM_MCP_URL"),
        help="Platform MCP endpoint (default: PLATFORM_MCP_URL env).",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("PLATFORM_CHAOS_TOKEN"),
        help=(
            "Bearer token with chaos:invoke scope (default: PLATFORM_CHAOS_TOKEN "
            "env). NOT the agent's PLATFORM_TOKEN, which no longer carries it."
        ),
    )

    sub = parser.add_subparsers(dest="command", required=True)

    kill = sub.add_parser(
        "kill-consumer",
        help="Shut down one Kafka consumer group. Fixed by restart_consumer_group.",
    )
    kill.add_argument("--group", default="worker-dispatcher")
    kill.add_argument("--ttl-seconds", type=int, default=300)

    poison = sub.add_parser(
        "poison-message",
        help="Send a schema-invalid Kafka message. Grows the DLQ; fixed by replay_dlq_messages.",
    )
    poison.add_argument("--topic", default="job.submitted")
    poison.add_argument(
        "--payload-json",
        default="{}",
        help="Payload JSON. Defaults to `{}` which fails every topic schema.",
    )
    poison.add_argument("--partition-key", default=None)

    sat = sub.add_parser(
        "saturate-redis",
        help="Pressure Redis with many short-TTL keys. Fixed by invalidate_cache_key.",
    )
    sat.add_argument("--num-keys", type=int, default=1000)
    sat.add_argument("--value-bytes", type=int, default=1024)
    sat.add_argument("--ttl-seconds", type=int, default=60)

    latency = sub.add_parser(
        "inject-latency",
        help="Slow one consumer group. Lag grows without a full kill.",
    )
    latency.add_argument("--group", default="worker-dispatcher")
    latency.add_argument("--ms", type=int, default=2000)
    latency.add_argument("--ttl-seconds", type=int, default=300)

    bad = sub.add_parser(
        "bad-deploy",
        help="Fire a critical alert + set a Redis bad-deploy flag.",
    )
    bad.add_argument("--label", default="chaos:bad_deploy")
    bad.add_argument("--ttl-seconds", type=int, default=600)
    bad.add_argument("--note", default=None)

    restore = sub.add_parser(
        "restore-consumer",
        help=(
            "Convenience: clear the kill/latency flags on one consumer group. "
            "Same effect as the agent's restart_consumer_group but invoked "
            "by the operator, not the agent, so live-eval starts clean."
        ),
    )
    restore.add_argument("--group", default="worker-dispatcher")

    bad_data = sub.add_parser(
        "bad-data-job",
        help=(
            "Create a synthetic bad-data DLQ entry — a persistent data bug the "
            "agent should fence with mark_dlq_permanent, not replay. Defaults "
            "to the UNCLASSIFIED form (remediation_hint=null), which is what "
            "`dlq_human_required_escalates` seeds. v0.4.0+; fixture_name and "
            "remediation_hint need v0.6.2+."
        ),
    )
    bad_data.add_argument("--job-type", default="csv_upload")
    # v0.6.2 (plat #198): the row's id is uuid5(dddddddd-bad0-4000-8000-000000000000,
    # "{tenant_id}:{fixture_name}"). Matches the scenario's own chaos_setup.
    bad_data.add_argument("--fixture-name", default="human-required-eval")
    # `unclassified` (JSON null), NOT the hook's `human_required` default: a row seeded
    # already-classified makes the fence a no-op (dlq_human_required_escalates.yaml).
    bad_data.add_argument(
        "--remediation-hint",
        default="unclassified",
        choices=["unclassified", "human_required"],
    )
    # Unset so the platform's `lab/dlq_failure_stories.py` stays the single source of the
    # text — the copy that lived in this default went stale at v0.6.1.
    bad_data.add_argument("--error-message", default=None)

    return parser


def main() -> int:
    """Fire the requested hook, print what the platform did, and say what to run next."""
    parser = _build_parser()
    args = parser.parse_args()

    if not args.mcp_url or not args.token:
        parser.error(
            "PLATFORM_MCP_URL and PLATFORM_CHAOS_TOKEN must be set (env or "
            "--flag). PLATFORM_CHAOS_TOKEN is the evaluator's own principal "
            "(incident-commander-chaos, chaos:invoke) and is NOT the agent's "
            "PLATFORM_TOKEN, which no longer carries that scope: run "
            "`make bootstrap-token` and paste both printed lines into .env."
        )

    client = ChaosClient(args.mcp_url, args.token)
    try:
        if args.command == "kill-consumer":
            result = client.call(
                "kill_consumer",
                {"consumer_group": args.group, "ttl_seconds": args.ttl_seconds},
            )
            _print_result("kill-consumer", result)
            print(
                f"\nNext: `make eval-live` and watch scenario "
                "`remediate_consumer_lag_success` — the agent should probe "
                f"lag on {args.group}, see it high, restart the group, and "
                "verify recovery."
            )
        elif args.command == "poison-message":
            payload = json.loads(args.payload_json)
            result = client.call(
                "poison_message",
                {
                    "topic": args.topic,
                    "payload": payload,
                    "partition_key": args.partition_key,
                },
            )
            _print_result("poison-message", result)
            print(
                "\nNext: wait ~30s for the consumer to DLQ the message, then "
                "run scenario `remediate_dlq_backlog_success` — the agent "
                "should replay the DLQ contents."
            )
        elif args.command == "saturate-redis":
            result = client.call(
                "saturate_redis",
                {
                    "num_keys": args.num_keys,
                    "value_bytes": args.value_bytes,
                    "ttl_seconds": args.ttl_seconds,
                },
            )
            _print_result("saturate-redis", result)
            print(
                "\nNext: agent scenario `redis_saturation` (read-only) or "
                "`remediate_stale_cache_success` (writes) will surface + "
                "respond to the pressure."
            )
        elif args.command == "inject-latency":
            result = client.call(
                "inject_latency",
                {
                    "consumer_group": args.group,
                    "latency_ms": args.ms,
                    "ttl_seconds": args.ttl_seconds,
                },
            )
            _print_result("inject-latency", result)
            print(
                f"\nNext: lag on {args.group} will grow over ~1 minute. "
                "Run `remediate_consumer_lag_success` — restart clears the "
                "latency by dropping the consumer's Redis state."
            )
        elif args.command == "bad-deploy":
            args_dict: dict[str, Any] = {
                "label": args.label,
                "ttl_seconds": args.ttl_seconds,
            }
            if args.note:
                args_dict["note"] = args.note
            result = client.call("bad_deploy", args_dict)
            _print_result("bad-deploy", result)
            print(
                "\nNext: agent should see the new alert via `list_active_alerts` "
                "and correlate against `get_deploy_history` — scenario "
                "`deploy_correlation` exercises that path."
            )
        elif args.command == "restore-consumer":
            # Not a chaos hook — the agent's Tier-1 tool, operator-driven. Fresh key per
            # invocation: a fixed one made every repeat a cache replay that deleted nothing.
            key = f"restore-consumer-cli-{uuid.uuid4().hex}"
            result = client.call(
                "restart_consumer_group",
                {"consumer_group": args.group, "idempotency_key": key},
            )
            _print_result("restore-consumer", result)
        elif args.command == "bad-data-job":
            arguments: dict[str, object] = {
                "job_type": args.job_type,
                "fixture_name": args.fixture_name,
                "remediation_hint": args.remediation_hint,
            }
            # Omitted rather than null: the platform's default depends on the declared hint,
            # and null would assert a text this caller has not chosen.
            if args.error_message is not None:
                arguments["error_message"] = args.error_message
            result = client.call("create_bad_data_job", arguments)
            _print_result("bad-data-job", result)
            print(
                "\nNext: run scenario `dlq_human_required_escalates` — the "
                "agent should probe list_dlq_messages UNFILTERED (no hint "
                "filter selects an unclassified row), read the bad-data error, "
                "classify it, fence it with mark_dlq_permanent, and escalate. "
                "The run ends ESCALATED: a fence stabilizes, it does not "
                "resolve."
            )
        else:
            parser.error(f"unknown command: {args.command}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
