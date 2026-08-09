"""The trace cost estimator must survive real trace shapes (WO-C4-03, A-12).

Two failure modes are pinned here:

* ``usage.get("cache_read_input_tokens", 0)`` returns ``None`` — not the
  default — when the key is present with a JSON null, which the Anthropic
  SDK emits because the cache usage fields are ``Optional``. ``row[3] += None``
  then aborts the whole audit on one record.
* Mixed-vintage append-only files must partition by ``invocation_id`` using
  exactly the same key as ``scripts/format_traces.py`` and
  ``evals/runner.py::_archive_trace_slice`` — the three tools disagreeing
  about what "no invocation id" means would silently split or merge attempts.

All fixtures are synthetic JSONL under ``tmp_path``; nothing runs the eval
harness and nothing is written under ``evals/`` (ADR 0011 freeze).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from incident_commander.llm.pricing import MODEL_PRICING
from scripts import estimate_cost
from scripts.estimate_cost import PRE_INVOCATION_ID, RATES, main


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _llm_record(
    usage: dict[str, Any],
    *,
    invocation_id: str | None = "inv1",
    model: str = "claude-sonnet-4-6",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": "llm",
        "role": "planner",
        "timestamp": "2026-08-01T10:00:05+00:00",
        "request": {"model": model},
        "response": {"usage": usage},
    }
    record["invocation_id"] = invocation_id
    return record


def _run(monkeypatch: pytest.MonkeyPatch, trace_dir: Path, *args: str) -> int:
    monkeypatch.setattr("sys.argv", ["estimate_cost.py", *args, str(trace_dir)])
    return main()


def test_null_cache_usage_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON-null cache fields contribute 0 instead of raising TypeError."""
    _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _llm_record(
                {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "cache_read_input_tokens": None,
                    "cache_creation_input_tokens": None,
                }
            )
        ],
    )

    assert _run(monkeypatch, tmp_path) == 0
    assert "ESTIMATE (lower bound):" in capsys.readouterr().out


def test_null_input_and_output_tokens_also_coerce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _llm_record(
                {
                    "input_tokens": None,
                    "output_tokens": None,
                    "cache_read_input_tokens": 10,
                    "cache_creation_input_tokens": 20,
                }
            )
        ],
    )

    assert _run(monkeypatch, tmp_path) == 0
    assert "ESTIMATE (lower bound):" in capsys.readouterr().out


def test_null_invocation_id_groups_under_the_shared_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A present-but-null invocation_id is pre-vintage, not a group called "None"."""
    _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [_llm_record({"input_tokens": 5, "output_tokens": 2}, invocation_id=None)],
    )

    assert _run(monkeypatch, tmp_path) == 0
    out = capsys.readouterr().out
    assert PRE_INVOCATION_ID in out
    assert "None" not in out


def test_missing_trace_dir_says_so_instead_of_reporting_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A silent $0.00 for a mistyped directory is worse than a loud notice."""
    assert _run(monkeypatch, tmp_path / "nope") == 0
    assert "no trace files" in capsys.readouterr().out.lower()


def test_partial_archive_trace_slice_is_priceable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A killed run's archive has no report.json — its billed calls still count (ADR 0017)."""
    archive = tmp_path / "runs" / "deadbeefcafe"
    slice_dir = archive / "traces"
    _write_jsonl(
        slice_dir / "redis_saturation.jsonl",
        [
            _llm_record(
                {
                    "input_tokens": 1_000_000,
                    "output_tokens": 0,
                    "cache_read_input_tokens": None,
                    "cache_creation_input_tokens": None,
                },
                invocation_id="deadbeefcafe",
            )
        ],
    )

    assert _run(monkeypatch, slice_dir) == 0
    assert not (archive / "report.json").exists()
    out = capsys.readouterr().out
    assert "deadbeefcafe" in out
    assert "$   3.00" in out


def test_rates_agree_with_the_pinned_pricing_module() -> None:
    """Single source of truth: no silent drift from ``llm/pricing.py``.

    The estimator keeps its own tier table because it prices HISTORICAL trace
    records, including model ids the runtime meter no longer pins. Where the
    two overlap they must agree to the cent, or a cost audit and the budget
    meter would report different numbers for the same call.
    """
    for model, row in MODEL_PRICING.items():
        tier = next(name for name in RATES if name in model)
        p_in, p_out, p_cr, p_cw = RATES[tier]
        assert p_in * 1e6 == pytest.approx(float(row.input_usd_per_mtok))
        assert p_out * 1e6 == pytest.approx(float(row.output_usd_per_mtok))
        assert p_cr * 1e6 == pytest.approx(float(row.cache_read_usd_per_mtok))
        assert p_cw * 1e6 == pytest.approx(float(row.cache_write_usd_per_mtok))


def test_docstring_describes_the_append_only_tracer() -> None:
    """A-12: the rationale text still claimed the pre-F-002 truncating tracer."""
    doc = estimate_cost.__doc__ or ""
    assert "truncated per run" not in doc
    assert "run-ledger.md" not in doc
    assert "study/runs.jsonl" in doc
    assert "append-only" in doc.lower()
