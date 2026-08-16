"""The external KiCad fixture corpus, located by ``RATSNESTPRO_FIXTURE_HOME``.

What is in it
-------------
911 files harvested from three upstream projects -- KiCad's own ``qa/data``,
KiBot's ``tests/board_samples``, and ``kicad-templates`` -- with a manifest
(``fixtures.json``) recording per file whether it parses, which KiCad format
version it is, and what role the upstream project gives it. 609 parse.

The corpus lives outside this repository and outside OneDrive: it is 160 MB of
third-party GPL data, and nothing here copies any of it.

``role`` means the opposite of what it sounds like
-------------------------------------------------
Read this before writing an assertion against the corpus:

``positive``
    Upstream asserts a *defect* here -- ``asserted in test_erc_ground_pins.cpp``.
    These are the files a checker is supposed to fire on.
``negative``
    Upstream expects this to be *clean* -- ``filename marker '_ok'``, or a
    working input its CI consumes. These are the false-positive baseline.
``excluded``
    No upstream assertion and no filename marker, so the role is unknown. 458 of
    the 609 are here, and nothing should be concluded from them in either
    direction.

What this corpus can and cannot show
------------------------------------
It cannot evidence any fact-sheet-based check. Measured 2026-08-03: of 1988
components across the 337 parsable schematics, 7 resolve to one of the 17 fact
sheets (0.35%), and of 99 project directories, **zero** contain an LDO or DC-DC
converter with a sheet. A gate like ``supply_pin_conflicts`` therefore never
reaches a verdict here, and a clean sweep says nothing about it. The values
explain why: the most common are ``R``, ``10K``, ``1k``, ``100R`` -- KiCad's
minimal QA sheets, not boards with order codes.

It does evidence topology checks, which need no part identity. The sharpest
subset is ``ground_pin_test_*``: four sheets upstream asserts
``ERCE_GROUND_PIN_NOT_GROUND`` on plus one ``_ok`` companion, which is the
paired-sample shape that is otherwise missing entirely.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

FIXTURE_HOME_ENV = "RATSNESTPRO_FIXTURE_HOME"

# Files the manifest accepts but that cannot serve as a false-positive baseline,
# because they are not circuits. Kept with the reason, and with what would make
# the exclusion wrong, so a corpus refresh can re-check them.
#
# Deliberately separate from ``kicad_demos.KNOWN_MALFORMED``. That list means
# "the bytes upstream ships are broken", so an entry leaving it is news about the
# upstream project. These two mean "this file cannot serve as evidence", which is
# not a claim about the file's validity at all. See the note beside
# ``KNOWN_MALFORMED`` for why they were not merged.
NOT_A_CIRCUIT: dict[str, str] = {
    "kibot/tests/board_samples/kicad_9/off-grid.kicad_sch": (
        "one resistor with both terminals on one net; it is KiBot's off-grid "
        "placement fixture, not a circuit. Upstream's own manifest entry says "
        "'measured triggerable: two_pin'. two_terminal_not_shorted is right "
        "about it and would still be right if the file were a real design"
    ),
    "kibot/tests/board_samples/kicad_10/off-grid.kicad_sch": (
        "same file, KiCad 10 copy of the same KiBot fixture"
    ),
}

# Files KiCad's own netlister refuses. Nothing downstream can be judged on a
# file the upstream tool will not load.
CLI_REJECTED: dict[str, str] = {
    "kicad/qa/data/pcbnew/issue24543/issue24543.kicad_sch": (
        "kicad-cli sch export netlist exits 3 (failed to load schematic); the "
        "upstream assertion on this case is a board-level DRCE_CREEPAGE test "
        "and the schematic is incidental to it"
    ),
    "kicad/qa/data/pcbnew/issue24544/issue24544.kicad_sch": (
        "kicad-cli sch export netlist exits 3, same upstream creepage case"
    ),
}


@dataclass(frozen=True)
class Fixture:
    """One manifest entry, resolved to a path on this machine."""

    path: Path
    relative: str
    role: str
    kind: str
    source: str
    evidence: str
    expected_codes: tuple[str, ...]

    @property
    def is_root_sheet(self) -> bool:
        """Whether a ``.kicad_pro`` sits beside it.

        Only a root sheet can be handed to ``kicad-cli sch export netlist``: the
        exporter resolves the hierarchy itself, and a child sheet exported alone
        yields a partial view.
        """
        return self.path.with_suffix(".kicad_pro").is_file()


@lru_cache(maxsize=1)
def fixture_home() -> Path | None:
    """The corpus root, or None when the environment does not name one.

    Never defaulted to a hard-coded path: the corpus is deliberately outside the
    repository, so a wrong guess would silently test nothing.
    """
    raw = os.environ.get(FIXTURE_HOME_ENV, "").strip()
    if not raw:
        return None
    root = Path(raw)
    return root if (root / "fixtures.json").is_file() else None


@lru_cache(maxsize=1)
def manifest() -> dict[str, Any] | None:
    root = fixture_home()
    if root is None:
        return None
    data = json.loads((root / "fixtures.json").read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


@lru_cache(maxsize=8)
def fixtures(
    *,
    role: str | None = None,
    kind: str | None = None,
    roots_only: bool = False,
) -> tuple[Fixture, ...]:
    """Manifest entries that parse, filtered and resolved.

    ``parse_ok`` is honoured rather than re-derived: the manifest records why each
    of the 302 rejected files was rejected (mostly a format version below the
    KiCad 9 floor, plus one malformed file with trailing data after the top-level
    expression), and duplicating that judgement here would let the two drift.

    Entries in :data:`NOT_A_CIRCUIT` and :data:`CLI_REJECTED` are dropped, so a
    caller cannot accidentally build a baseline on them.
    """
    data = manifest()
    root = fixture_home()
    if data is None or root is None:
        return ()
    out: list[Fixture] = []
    for entry in data.get("fixtures") or ():
        if not entry.get("parse_ok"):
            continue
        relative = str(entry.get("resolved_path") or entry.get("path") or "")
        if not relative or relative in NOT_A_CIRCUIT or relative in CLI_REJECTED:
            continue
        if role is not None and entry.get("role") != role:
            continue
        if kind is not None and entry.get("kind") != kind:
            continue
        path = root / relative
        if not path.is_file():
            continue
        fixture = Fixture(
            path=path,
            relative=relative,
            role=str(entry.get("role") or ""),
            kind=str(entry.get("kind") or ""),
            source=str(entry.get("source") or ""),
            evidence=str(entry.get("role_evidence") or ""),
            expected_codes=tuple(str(c) for c in entry.get("expected_codes") or ()),
        )
        if roots_only and not fixture.is_root_sheet:
            continue
        out.append(fixture)
    return tuple(out)


def ground_pin_cases() -> tuple[Fixture, ...]:
    """KiCad's own ``ERCE_GROUND_PIN_NOT_GROUND`` cases and their clean companion.

    Five sheets that differ only in how ground is wired, four of them asserted
    defective upstream and one named ``_ok``. Returned together because the value
    is in the pairing: firing on all five would look identical to working.
    """
    return tuple(
        f
        for f in fixtures(kind="schematic")
        if f.path.name.startswith("ground_pin_test_")
    )


requires_fixture_home = pytest.mark.skipif(
    fixture_home() is None,
    reason=(
        f"{FIXTURE_HOME_ENV} is not set to a directory containing fixtures.json; "
        "the external corpus is 160 MB of third-party data kept outside this repo"
    ),
)
