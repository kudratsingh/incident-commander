"""A written snapshot must never be reported as a crash (WO-R2-99).

``snapshot_platform_tools.py`` writes the file and THEN prints where it
went, via ``out_path.relative_to(_REPO_ROOT)``. ``relative_to`` raises
``ValueError`` for any path that is not under the repo root — which
includes both ``--out /tmp/x.json`` (an operator dumping a snapshot to
compare two platform versions by hand) and a plain relative
``--out out.json``, since the unresolved ``Path("out.json")`` is not
under the absolute repo root either. The snapshot was already on disk by
then, so the operator saw a traceback for a run that had succeeded and
re-ran it against a live platform for no reason.

Offline: ``fetch_tools`` is stubbed, so nothing here talks to a platform
and nothing touches the committed snapshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import snapshot_platform_tools
from scripts.snapshot_platform_tools import main

_REPO_ROOT = Path(snapshot_platform_tools.__file__).resolve().parents[1]

_ONE_TOOL: dict[str, object] = {
    "tools": [
        {
            "name": "get_consumer_lag",
            "description": "Read consumer lag.",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        snapshot_platform_tools, "fetch_tools", lambda _url, _token: dict(_ONE_TOOL)
    )


def test_an_out_path_outside_the_repo_still_reports_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "x.json"
    code = main(["--mcp-url", "http://platform.test/mcp", "--token", "tok", "--out", str(out)])
    assert code == 0
    assert out.exists(), "the snapshot is written before the print — it must survive"
    printed = capsys.readouterr().out
    assert "wrote 1 tools" in printed
    assert str(out) in printed, "an out-of-repo path must be named in full, not crash"


def test_a_relative_out_path_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--out out.json`` from any cwd: unresolved, it is not under the repo
    root, so the unguarded ``relative_to`` raised on it too."""
    monkeypatch.chdir(tmp_path)
    assert main(["--mcp-url", "http://p/mcp", "--token", "tok", "--out", "out.json"]) == 0
    assert (tmp_path / "out.json").exists()
    assert "wrote 1 tools" in capsys.readouterr().out


def test_an_in_repo_path_still_prints_the_short_form(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tidy repo-relative rendering is the whole point of the guard being
    a fallback rather than a replacement — keep it for the default case."""
    monkeypatch.chdir(_REPO_ROOT)
    out = _REPO_ROOT / "contracts" / "platform-tools.snapshot.json"
    monkeypatch.setattr(
        snapshot_platform_tools,
        "_SNAPSHOT_PATH",
        tmp_path / "snap.json",
    )
    # Name the real in-repo path without writing to it: point --out at a
    # scratch file under the repo that the test owns and removes.
    scratch = _REPO_ROOT / "contracts" / ".wo-r2-99-scratch.json"
    try:
        assert main(["--mcp-url", "http://p/mcp", "--token", "tok", "--out", str(scratch)]) == 0
        printed = capsys.readouterr().out
        assert "contracts/.wo-r2-99-scratch.json" in printed
        assert str(_REPO_ROOT) not in printed, "in-repo paths stay repo-relative"
    finally:
        scratch.unlink(missing_ok=True)
    assert out.exists(), "the committed snapshot must be untouched"
