"""Drift tripwires for the demo stack: pinning, named volumes, and the README.

Three defects motivated this file (findings C-10, C-11, S-01), all of the same
shape: the demo stack's *documented* and *declared* behavior drifted away from
what `make demo-down` and `demo/compose.yml` actually do.

* C-11 — both redpanda services floated on ``redpandadata/redpanda:latest``
  while every platform service is digest-pinned, so broker and rpk client could
  resolve different builds on different days.
* S-01 — redpanda declared no volume. Its image declares
  ``VOLUME /var/lib/redpanda/data``, so every ``docker compose down`` orphaned
  an anonymous volume and took the Kafka event log and consumer-group offsets
  with it — the same destructive-stop mechanism #84 fixed for postgres/redis.
* C-10 — demo/README.md still told readers "Volumes are wiped on demo-down"
  and inlined a platform digest that had gone stale two releases earlier. This
  repo's established failure mode is a coding agent "fixing" code to match a
  stale doc, which here means re-adding ``-v`` and destroying the audit log
  that CLAUDE.md invariant 6 makes the ground truth for grading safety.

The README rule is *single-source*: it must not inline a digest at all — the
``image:`` lines in demo/compose.yml are the only copy that can be right.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import yaml

_REPO: Final[Path] = Path(__file__).resolve().parents[2]
_COMPOSE: Final[Path] = _REPO / "demo" / "compose.yml"
_README: Final[Path] = _REPO / "demo" / "README.md"

_REDPANDA_SERVICES: Final[tuple[str, ...]] = ("redpanda", "redpanda-init")
# Declared as VOLUME in the redpandadata/redpanda image config, so an
# unnamed mount here is an anonymous volume, not container-layer storage.
_REDPANDA_DATA_DIR: Final[str] = "/var/lib/redpanda/data"

# repository:tag@sha256:<64 hex> — tag kept for humans, digest is what pulls.
_DIGEST_PINNED: Final[re.Pattern[str]] = re.compile(r"^[^\s@]+:[^\s@:]+@sha256:[0-9a-f]{64}$")


def _services() -> dict[str, dict[str, object]]:
    """The compose file's ``services`` mapping, one entry per service."""
    document: object = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{_COMPOSE.name} did not parse as a mapping"
    services = document.get("services")
    assert isinstance(services, dict), f"{_COMPOSE.name} declares no services mapping"
    return {str(name): body for name, body in services.items() if isinstance(body, dict)}


def _image_refs() -> dict[str, str]:
    """service name -> ``image:`` reference, for every service that declares one."""
    refs: dict[str, str] = {}
    for name, body in _services().items():
        image = body.get("image")
        if isinstance(image, str):
            refs[name] = image
    return refs


def _top_level_volume_names() -> frozenset[str]:
    document: object = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{_COMPOSE.name} did not parse as a mapping"
    volumes = document.get("volumes")
    assert isinstance(volumes, dict), f"{_COMPOSE.name} declares no top-level volumes block"
    return frozenset(str(name) for name in volumes)


def _service_mounts(service: str) -> list[str]:
    body = _services()[service]
    declared = body.get("volumes")
    if declared is None:
        return []
    assert isinstance(declared, list), f"{service}.volumes is not a list"
    return [entry for entry in declared if isinstance(entry, str)]


def test_compose_canary() -> None:
    """Guard against vacuous passes: the services this file lints must exist."""
    refs = _image_refs()
    assert refs, f"layout canary: no image references found in {_COMPOSE}"
    missing = [name for name in _REDPANDA_SERVICES if name not in refs]
    assert missing == [], f"layout canary: compose no longer declares {missing}"


def test_no_image_floats_on_latest() -> None:
    """C-11: a mutable ``:latest`` moves under the stack between pulls."""
    offenders = sorted(
        f"{name}: {ref}" for name, ref in _image_refs().items() if ref.endswith(":latest")
    )
    assert offenders == [], (
        "demo/compose.yml must never pull `:latest` — the demo stack is the "
        f"artifact the evals and the CI contract job run against: {offenders}"
    )


def test_redpanda_is_pinned_by_digest() -> None:
    """C-11: pin the broker AND the rpk client that talks to it."""
    refs = _image_refs()
    unpinned = sorted(
        f"{name}: {refs[name]}"
        for name in _REDPANDA_SERVICES
        if _DIGEST_PINNED.fullmatch(refs[name]) is None
    )
    assert unpinned == [], (
        "redpanda must be pinned as repository:tag@sha256:<digest> (the tag "
        f"stays for humans, the digest is what actually pulls): {unpinned}"
    )


def test_every_repository_resolves_to_one_ref() -> None:
    """Two services on one repository must agree — skew is the failure mode.

    Covers the redpanda broker/init pair (rpk client/broker version skew) and
    the three platform services, whose digests move together or not at all.
    """
    by_repository: dict[str, set[str]] = {}
    for ref in _image_refs().values():
        by_repository.setdefault(ref.split(":", 1)[0].split("@", 1)[0], set()).add(ref)
    skewed = sorted(repo for repo, refs in by_repository.items() if len(refs) > 1)
    assert skewed == [], (
        f"these repositories are referenced with more than one ref: {skewed}. "
        "Services sharing an image must share the exact ref string."
    )


def test_redpanda_data_lives_on_a_named_volume() -> None:
    """S-01: `make demo-down` must not delete the Kafka event log."""
    mounts = _service_mounts("redpanda")
    # Parse the mount rather than string-matching its tail. Compose short
    # syntax is source:target[:mode], so `endswith(":/var/lib/redpanda/data")`
    # called a perfectly valid `demo_redpandadata:/var/lib/redpanda/data:rw`
    # unnamed — failing with a message claiming the event log was about to be
    # destroyed by a configuration that destroys nothing (WO-R2-102).
    named = [
        mount
        for mount in mounts
        if mount.split(":")[1:2] == [_REDPANDA_DATA_DIR] and not mount.startswith((".", "/"))
    ]
    assert named, (
        f"redpanda declares VOLUME {_REDPANDA_DATA_DIR}; without a named volume "
        "there, every `docker compose down` orphans an anonymous volume and "
        f"destroys the event log and consumer offsets. Mounts: {mounts}"
    )
    declared = _top_level_volume_names()
    undeclared = sorted(
        mount.split(":", 1)[0] for mount in named if mount.split(":", 1)[0] not in declared
    )
    assert undeclared == [], (
        f"named volumes must also appear in the top-level volumes block: {undeclared}"
    )


def test_readme_inlines_no_digest() -> None:
    """C-10: compose.yml is the single source for the pinned digest.

    The README inlined ``sha256:a1940afa…`` and went stale two releases later.
    Referencing compose.yml cannot go stale; a second copy always can.
    """
    hits = [
        line.strip()
        for line in _README.read_text(encoding="utf-8").splitlines()
        if "sha256:" in line
    ]
    assert hits == [], (
        "demo/README.md must not inline an image digest — point at the "
        f"`image:` lines in demo/compose.yml instead: {hits}"
    )


def test_readme_documents_demo_down_as_non_destructive() -> None:
    """C-10: the README must not invite anyone to re-add ``down -v``."""
    text = _README.read_text(encoding="utf-8")
    assert re.search(r"wiped on", text, re.IGNORECASE) is None, (
        "demo/README.md still claims volumes are wiped on demo-down. `make "
        "demo-down` is `docker compose down` (no -v) since #84: stopping a "
        "stack must not be a destructive act."
    )
    keeps = [
        line
        for line in text.splitlines()
        if "demo-down" in line and re.search(r"keeps?\b", line, re.IGNORECASE)
    ]
    assert keeps, "demo/README.md must state that `make demo-down` KEEPS the data"
    for required in ("demo-destroy", "CONFIRM=1"):
        assert required in text, (
            f"demo/README.md must name `{required}` as the explicit, "
            "irreversible way to wipe the demo data"
        )
