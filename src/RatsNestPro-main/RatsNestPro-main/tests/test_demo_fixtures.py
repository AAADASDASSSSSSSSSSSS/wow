"""The static corpora are usable: every fixture parses and yields real data.

This is the floor the later electrical checks stand on. If a demo board stops
loading after a KiCad upgrade, or the vendored parser regresses, the failure
should surface here — as "the corpus broke" — rather than as a confusing false
ERROR from a check that never got a netlist to look at.

The corpus is parsed once per session via ``kicad_demos.parsed_*``; these tests
assert over the cached result instead of re-reading the files.
"""

from __future__ import annotations

import pytest

from ratsnestpro.eda.vendor.pcb import PcbBoard
from tests.fixtures import kicad_demos as demos


@demos.requires_demos
def test_demo_corpus_is_not_trivially_small() -> None:
    """A corpus of two boards would not prove much about false positives."""
    projects = demos.demo_projects()
    assert len(projects) >= 10, f"only {len(projects)} demo projects discovered"
    assert len(demos.demo_boards()) >= 10
    assert len(demos.demo_schematics()) >= 10


@demos.requires_demos
def test_every_demo_schematic_parses_and_lists_components() -> None:
    populated = 0
    for _path, doc in demos.parsed_schematics():
        if doc.list_components():
            populated += 1
    # Some sheets in a hierarchy are pure interconnect, so not every sheet has
    # components; the corpus as a whole must.
    assert populated >= 10, f"only {populated} sheets reported components"


@demos.requires_demos
def test_connectivity_graph_builds_for_every_demo_schematic() -> None:
    """Checks read nets, so graph construction — not just parsing — must work."""
    graphs = demos.schematic_graphs()
    assert len(graphs) == len(demos.demo_schematics())
    with_nets = sum(1 for _p, g in graphs if any(c["nets"] for c in g.components()))
    assert with_nets >= 10, f"only {with_nets} sheets yielded named nets"


@demos.requires_demos
def test_every_default_tier_board_parses_and_carries_nets() -> None:
    boards = demos.parsed_boards()
    assert len(boards) >= 10
    with_nets = sum(1 for _p, b in boards if b.list_nets())
    assert with_nets >= 10, f"only {with_nets} demo boards reported nets"


@demos.requires_demos
def test_known_malformed_boards_are_excluded_but_recorded() -> None:
    """Exclusions must stay visible, so a shrinking corpus cannot pass unnoticed."""
    listed = {b.name for b in demos.demo_boards(include_malformed=True, include_oversized=True)}
    default = {b.name for b in demos.demo_boards()}
    for name, reason in demos.KNOWN_MALFORMED.items():
        if name not in listed:
            continue  # not shipped by this KiCad version
        assert name not in default
        assert reason.strip(), f"{name} excluded without a reason"


@demos.requires_demos
def test_recorded_malformed_boards_still_fail_to_parse() -> None:
    """If upstream fixes the file, the exclusion should be deleted, not kept forever."""
    checked = 0
    for board in demos.demo_boards(include_malformed=True, include_oversized=True):
        if board.name not in demos.KNOWN_MALFORMED:
            continue
        checked += 1
        with pytest.raises(Exception):  # noqa: B017 - any parse failure is the point
            PcbBoard.load(board)
    if not checked:
        pytest.skip("no recorded-malformed board is present in this KiCad version")


@demos.requires_demos
def test_oversized_boards_are_deferred_not_dropped() -> None:
    """The giant boards must remain reachable, just not in the default sweep."""
    default = set(demos.demo_boards())
    everything = set(demos.demo_boards(include_oversized=True))
    deferred = everything - default
    for board in deferred:
        assert demos.is_oversized(board)
    # Not asserting that deferred is non-empty: a future KiCad may ship smaller
    # demos, and that would be an improvement rather than a regression.
    assert default, "the default tier must not be empty"


@demos.requires_demos
def test_hierarchical_and_paired_projects_are_present() -> None:
    """The corpus keeps the variety that makes it worth using."""
    projects = demos.demo_projects()
    assert any(len(p.schematics) >= 4 for p in projects), "no hierarchical project"
    assert any(len(p.boards) >= 2 for p in projects), "no multi-board project"


@pytest.mark.slow_corpus
@demos.requires_demos
def test_oversized_boards_parse() -> None:
    """Opt-in: the tens-of-megabyte demos, excluded from the default sweep."""
    oversized = [b for b in demos.demo_boards(include_oversized=True) if demos.is_oversized(b)]
    if not oversized:
        pytest.skip("no oversized demo board in this KiCad version")
    failures: list[str] = []
    for board in oversized:
        try:
            PcbBoard.load(board)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{board.name}: {type(exc).__name__}: {exc}")
    assert not failures, "oversized boards failed to parse:\n" + "\n".join(failures)


@demos.requires_positive_sample
def test_positive_sample_schematic_loads() -> None:
    """The known-defective design is the other half of every check's evidence."""
    from ratsnestpro.eda.vendor.schematic import Schematic

    schematic = demos.positive_sample_schematic()
    assert schematic is not None
    assert Schematic.load(schematic).list_components()


@demos.requires_positive_sample
def test_positive_sample_board_is_the_routed_one() -> None:
    board = demos.positive_sample_board()
    assert board is not None
    assert ".unrouted" not in board.name
    PcbBoard.load(board)
