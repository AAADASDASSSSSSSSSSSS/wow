"""Static fixture corpora: correct boards to prove a check does not misfire.

A new electrical check has two failure modes and they need different evidence.
It can miss the defect it was written for, which one known-bad design proves.
It can also fire on a perfectly good design, and *that* is the expensive
mistake — a false ERROR stops the pipeline, spends a repair round, and teaches
the AHE loop to chase a target that was never wrong. Proving absence of false
positives needs many correct boards, and generating them is not affordable.

KiCad ships sixteen of them. ``share/kicad/demos`` holds real, human-reviewed
projects with both schematics and boards, including hierarchical designs, a
routed/unrouted pair and two revisions of one board. They are referenced **by
discovered path and never copied into this repository**: licensing stays a
non-question, the repo stays small, and a host without KiCad simply skips the
tests instead of failing them.

Positive samples come from ``data/ratsnestpro/runs``, where a previous pipeline
run left a design whose defects are already diagnosed in ``cases/README.md``.
Both corpora are static — no LLM, no API quota, no Freerouting, no 20-minute
run — so a check can be regression-tested on every commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pytest

from ratsnestpro.eda.vendor import kicad_paths

# Upstream files that are not valid s-expressions. Excluding them silently would
# quietly shrink the corpus, so each one is listed with the reason and must be
# re-checked when KiCad is upgraded.
#
# RoyalBlue54L-Feather: the shipped file contains ``(curved_edges no)filter_ratio
# 0.9)`` inside a pad's ``teardrops`` block — an opening parenthesis is missing,
# so the top-level expression closes about 14 kB into a 3.5 MB file and every
# later footprint parses as a sibling of ``kicad_pcb``. The vendored parser is
# right to reject it.
KNOWN_MALFORMED: dict[str, str] = {
    "RoyalBlue54L-Feather.kicad_pcb": (
        "upstream file is malformed: missing '(' before filter_ratio in a "
        "teardrops block closes the top-level expression early"
    ),
}

# Why this is not one list shared with the external corpus
# -------------------------------------------------------
# ``tests/fixtures/kicad_fixtures.py`` keeps its own exclusions
# (``NOT_A_CIRCUIT``, ``CLI_REJECTED``) and additionally honours the manifest's
# ``parse_ok``, which is where its own malformed file lives — a ``qa/data``
# board with trailing text after the top-level expression. Merging the two was
# considered and rejected: these lists answer different questions. This one says
# "the bytes KiCad ships are broken", so an entry disappearing means the
# *upstream project* fixed something. The other says "this file cannot serve as
# evidence", which covers format age, non-circuits, and files kicad-cli refuses
# — none of which is a defect in the file. One list would make both reasons
# unfalsifiable.

# Boards above this size are excluded from the default sweep and covered by the
# ``slow_corpus`` marker instead. Two demos are tens of megabytes and cost about
# 100 of the 115 seconds a full board sweep takes, while adding nothing a
# schematic-level check can use. Measured on this corpus:
#   jetson-agx-thor-baseboard  86 MB  57 s
#   vme-wren                   70 MB  42 s
#   everything else combined   27 MB  15 s
_OVERSIZED_BOARD_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class DemoProject:
    """One demo directory with whatever artifacts it actually contains."""

    name: str
    directory: Path
    boards: tuple[Path, ...]
    schematics: tuple[Path, ...]

    @property
    def parsable_boards(self) -> tuple[Path, ...]:
        return tuple(b for b in self.boards if b.name not in KNOWN_MALFORMED)


def is_oversized(board: Path) -> bool:
    try:
        return board.stat().st_size > _OVERSIZED_BOARD_BYTES
    except OSError:
        return False

# The run whose defects cases/README.md documents. Relative to this file:
# tests/fixtures -> tests -> RatsNestPro-main -> RatsNestPro-main -> src -> repo.
_POSITIVE_SAMPLE_RUN = (
    Path(__file__).resolve().parents[5] / "data" / "ratsnestpro" / "runs" / "ratsnest-370639d2"
)


@lru_cache(maxsize=1)
def demos_root() -> Path | None:
    """The first discovered ``share/kicad/demos``, or None when KiCad is absent."""
    for candidate in kicad_paths.demo_dirs():
        if candidate.is_dir():
            return candidate
    return None


@lru_cache(maxsize=1)
def demo_projects() -> tuple[DemoProject, ...]:
    """Every demo project, grouped by its top-level directory.

    Grouping is by the directory directly under ``demos`` rather than by file so
    a hierarchical project with eight sheets counts once, and a sub-project like
    ``royalblue54L_feather/nfc_antenna`` stays with its parent.
    """
    root = demos_root()
    if root is None:
        return ()
    out: list[DemoProject] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        boards = tuple(sorted(directory.rglob("*.kicad_pcb")))
        schematics = tuple(sorted(directory.rglob("*.kicad_sch")))
        if boards or schematics:
            out.append(DemoProject(directory.name, directory, boards, schematics))
    return tuple(out)


def demo_boards(
    *, include_malformed: bool = False, include_oversized: bool = False
) -> tuple[Path, ...]:
    """Demo ``.kicad_pcb`` paths.

    Malformed and oversized boards are excluded by default; both exclusions are
    explicit and testable rather than a silently shorter list.
    """
    boards: list[Path] = []
    for project in demo_projects():
        for board in project.boards if include_malformed else project.parsable_boards:
            if include_oversized or not is_oversized(board):
                boards.append(board)
    return tuple(boards)


def demo_schematics() -> tuple[Path, ...]:
    """All demo ``.kicad_sch`` paths, including every sheet of a hierarchy."""
    schematics: list[Path] = []
    for project in demo_projects():
        schematics.extend(project.schematics)
    return tuple(schematics)


@lru_cache(maxsize=1)
def demo_root_schematics() -> tuple[Path, ...]:
    """One root sheet per project file — what a netlist export should be given.

    A hierarchy must be exported from its root: ``kicad-cli sch export netlist``
    resolves child sheets itself, so handing it each sheet in turn would both
    duplicate work and produce partial views for sheets that are pure
    interconnect. The root is identified by the ``.kicad_pro`` next to it rather
    than by directory position, because several demos nest sub-projects (the
    ``simulation`` directory holds about twenty).
    """
    root = demos_root()
    if root is None:
        return ()
    out: list[Path] = []
    for project_file in sorted(root.rglob("*.kicad_pro")):
        sheet = project_file.with_suffix(".kicad_sch")
        if sheet.is_file():
            out.append(sheet)
    return tuple(out)


# --------------------------------------------------------------------------- #
# Parsed-artifact cache
#
# The corpus is parsed once per pytest session and shared by every check that
# needs it. Without this each new electrical check would re-parse 115 sheets,
# and the cost would grow linearly with the number of checks — exactly the thing
# that would push the corpus out of the per-commit run and into disuse.
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=1)
def parsed_schematics() -> tuple[tuple[Path, object], ...]:
    """Every demo schematic, parsed. Unparsable sheets are reported, not hidden."""
    from ratsnestpro.eda.vendor.schematic import Schematic

    out: list[tuple[Path, object]] = []
    for path in demo_schematics():
        out.append((path, Schematic.load(path)))
    return tuple(out)


@lru_cache(maxsize=1)
def schematic_graphs() -> tuple[tuple[Path, object], ...]:
    """Connectivity graph per demo schematic — what a netlist-level check reads."""
    from ratsnestpro.eda.vendor.connectivity import SchematicGraph

    return tuple((path, SchematicGraph(doc)) for path, doc in parsed_schematics())


@lru_cache(maxsize=1)
def parsed_boards() -> tuple[tuple[Path, object], ...]:
    """Every default-tier demo board, parsed."""
    from ratsnestpro.eda.vendor.pcb import PcbBoard

    return tuple((path, PcbBoard.load(path)) for path in demo_boards())


@lru_cache(maxsize=1)
def demo_netlists() -> tuple[tuple[Path, object], ...]:
    """``(root sheet, KicadNetlist)`` for every project, exported once per session.

    Each export is a ``kicad-cli`` process, roughly 2-25 s depending on project
    size and about 110 s for the whole corpus, so this must be shared rather than
    repeated per check. A project whose export fails is omitted from the result;
    :func:`netlist_export_failures` reports those separately so a shrinking
    corpus cannot pass unnoticed.
    """
    from ratsnestpro.eda.netlist import netlist_for_schematic

    out: list[tuple[Path, object]] = []
    for path in demo_root_schematics():
        try:
            out.append((path, netlist_for_schematic(path)))
        except Exception:  # noqa: BLE001 - recorded, then asserted on by the tests
            continue
    return tuple(out)


def netlist_export_failures() -> tuple[Path, ...]:
    """Root sheets that :func:`demo_netlists` could not export."""
    exported = {path for path, _ in demo_netlists()}
    return tuple(p for p in demo_root_schematics() if p not in exported)


def positive_sample_run() -> Path | None:
    """The known-defective run directory, or None when it is not on disk."""
    return _POSITIVE_SAMPLE_RUN if _POSITIVE_SAMPLE_RUN.is_dir() else None


def positive_sample_schematic() -> Path | None:
    run = positive_sample_run()
    if run is None:
        return None
    return next(iter(sorted(run.glob("*.kicad_sch"))), None)


def positive_sample_board() -> Path | None:
    run = positive_sample_run()
    if run is None:
        return None
    # The routed board, not the ``.unrouted`` snapshot beside it.
    boards = [p for p in sorted(run.glob("*.kicad_pcb")) if ".unrouted" not in p.name]
    return boards[0] if boards else None


# --------------------------------------------------------------------------- #
# Skip helpers
# --------------------------------------------------------------------------- #

requires_demos = pytest.mark.skipif(
    demos_root() is None,
    reason="KiCad demo corpus not found; install KiCad to run the negative-sample checks",
)

requires_positive_sample = pytest.mark.skipif(
    positive_sample_run() is None,
    reason=(
        "the known-defective run data/ratsnestpro/runs/ratsnest-370639d2 is not on disk; "
        "it is generated output, not committed"
    ),
)


@lru_cache(maxsize=1)
def _kicad_cli_found() -> bool:
    from ratsnestpro.eda.vendor.kicad_cli import KicadCliNotFound, find_kicad_cli

    try:
        find_kicad_cli()
    except KicadCliNotFound:
        return False
    return True


requires_kicad_cli = pytest.mark.skipif(
    not _kicad_cli_found(),
    reason="kicad-cli not found; netlist-based connectivity needs KiCad's own netlister",
)
