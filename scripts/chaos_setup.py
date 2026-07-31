#!/usr/bin/env python3
"""Chaos setup helper — put the live platform into a fixable state.

Live-eval remediation scenarios (``remediate_*``) assume something is
actually broken. When you invoke the agent against a healthy platform,
the initial probe returns clean, the remediation is a no-op, and the
verify judge trivially says "verified" — you get a green run that
doesn't prove much.

This CLI wraps the platform's chaos hooks so you can seed a real
failure right before ``make eval-live``. The 5 hooks map to the
Tier-1 remediations the agent knows about:

    kill_consumer     → agent should restart_consumer_group
    poison_message    → DLQ builds up; agent should replay_dlq_messages
    saturate_redis    → cache pressure; agent should invalidate_cache_key
    inject_latency    → lag grows without full kill; restart or wait
    bad_deploy        → fires alert; agent correlates via get_deploy_history

Every chaos effect self-cleans on TTL (default 5–10 min) so you can
walk away and the platform recovers on its own.

Usage:

    export PLATFORM_MCP_URL=http://localhost:8001/mcp
    export PLATFORM_TOKEN=sa_...           # needs chaos:invoke scope
    uv run python scripts/chaos_setup.py kill-consumer --group worker-dispatcher
    uv run python scripts/chaos_setup.py poison-message --topic job.submitted
    uv run python scripts/chaos_setup.py saturate-redis --num-keys 5000
    uv run python scripts/chaos_setup.py inject-latency --group worker-dispatcher --ms 2000
    uv run python scripts/chaos_setup.py bad-deploy
    uv run python scripts/chaos_setup.py restore-consumer --group worker-dispatcher

The token needs ``chaos:invoke`` scope. If your service-account token
was minted without it (default scopes are ``telemetry:read`` +
``incidents:read``), regenerate with ``scripts/bootstrap_agent_token.py``
passing ``--scope chaos:invoke``.

Chaos hooks are only registered when the platform boots with
``CHAOS_ENABLED=true``. That's on by default in demo/compose.yml; if
you're running an isolated platform, set it yourself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

_DEFAULT_TIMEOUT_SECONDS = 15.0


class ChaosClient:
    """Minimal JSON-RPC caller — deliberately not the agent's MCPClient.

    Chaos setup is an operator concern outside the agent's trust
    boundary. Using a raw httpx call keeps this script honest about
    that separation.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self._url = base_url.rstrip("/")
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )

    def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        response = self._client.post(self._url, json=body)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"non-object JSON response: {type(payload).__name__}")
        if "error" in payload:
            err = payload["error"]
            raise RuntimeError(
                f"platform returned MCP error {err.get('code')}: {err.get('message')}"
            )
        result = payload.get("result", {})
        if not isinstance(result, dict):
            return {}
        content = result.get("content", [])
        for block in content:
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parsed = json.loads(block["text"])
                if isinstance(parsed, dict):
                    return parsed
        return {}

    def close(self) -> None:
        self._client.close()


def _print_result(action: str, result: dict[str, Any]) -> None:
    print(f"[{action}] platform returned:")
    print(json.dumps(result, indent=2))


def _build_parser() -> argparse.ArgumentParser:
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
        default=os.environ.get("PLATFORM_TOKEN"),
        help="Bearer token with chaos:invoke scope (default: PLATFORM_TOKEN env).",
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
            "Create a synthetic DLQ entry hinted human_required — a persistent "
            "data bug the agent should mark permanent, not replay. v0.4.0+."
        ),
    )
    bad_data.add_argument("--job-type", default="csv_upload")
    bad_data.add_argument(
        "--error-message",
        default="ValueError: invalid literal for int() with base 10: 'not-a-number' at row 15,382",
    )

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.mcp_url or not args.token:
        parser.error(
            "PLATFORM_MCP_URL and PLATFORM_TOKEN must be set (env or --flag). "
            "Run `make bootstrap-token` first (add --scope chaos:invoke)."
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
            # Not a chaos hook — call the agent's Tier-1 tool directly to
            # clear leftover flags. Operator-driven; skips the propose/execute
            # ceremony because the agent isn't in the loop.
            key = "restore-consumer-cli-invocation-01"
            result = client.call(
                "restart_consumer_group",
                {"consumer_group": args.group, "idempotency_key": key},
            )
            _print_result("restore-consumer", result)
        elif args.command == "bad-data-job":
            result = client.call(
                "create_bad_data_job",
                {"job_type": args.job_type, "error_message": args.error_message},
            )
            _print_result("bad-data-job", result)
            print(
                "\nNext: run scenario `dlq_human_required_escalates` — the "
                "agent should probe list_dlq_messages, see hint=human_required, "
                "and mark_dlq_permanent instead of attempting replay."
            )
        else:
            parser.error(f"unknown command: {args.command}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
