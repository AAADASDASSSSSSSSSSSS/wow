"""``_ConnectivityView.from_schematic`` agrees with KiCad's own netlister.

Why this file exists at all
---------------------------
Its predecessor asserted the output of a view built from
``vendor.connectivity.SchematicGraph``, which unions wire *endpoints* and so
misses pins landing mid-segment. On ``pic_programmer`` that recovered 48 of 236
pin -> net entries and agreed on 25 of those 48 names, and it turned 34
non-existent shorts into expected values. A test that records wrong answers is
worse than no test, so it was deleted rather than updated.

The truth side here is extracted from the exporter's output by a line scanner
local to this module, not by :func:`ratsnestpro.eda.netlist.parse_netlist`. The
subject under test is the parse-and-assemble path; using it to produce its own
expectations would assert nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ratsnestpro.eda.netlist import NetlistError, export_netlist
from ratsnestpro.orchestration.pipeline import (
    _ConnectivityView,
    _two_terminal_short_checks,
)
from tests.fixtures import kicad_demos as demos

pytestmark = [demos.requires_demos, demos.requires_kicad_cli]

# The demo whose numbers the 2026-07-31 investigation is written against: two
# sheets, so it also proves the hierarchy is resolved, and small enough that all
# 236 entries can be read by hand.
_DEMO = "pic_programmer"

_QUOTED = r'"((?:[^"\\]|\\.)*)"'
_NET_NAME = re.compile(rf"^\t{{3}}\(name\s+{_QUOTED}\)")
_NODE_REF = re.compile(rf"^\t{{4}}\(ref\s+{_QUOTED}\)")
_NODE_PIN = re.compile(rf"^\t{{4}}\(pin\s+{_QUOTED}\)")


def _root_sheet(name: str) -> Path:
    for path in demos.demo_root_schematics():
        if path.parent.name == name:
            return path
    pytest.skip(f"demo project {name} not present")


def _nodes_by_indentation(netlist_text: str) -> dict[tuple[str, str], str]:
    """``(ref, pin) -> net name``, read by indentation depth alone.

    Deliberately naive and independent of the vendored s-expression parser. The
    exporter writes one field per line with tab depth fixed by nesting, so depth
    is enough to tell a net's own ``(name ...)`` from a pin name inside
    ``libparts``, provided scanning starts at the ``nets`` section.
    """
    out: dict[tuple[str, str], str] = {}
    lines = netlist_text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("\t(nets"))
    net = ""
    ref = ""
    for line in lines[start:]:
        if match := _NET_NAME.match(line):
            net = match.group(1)
            ref = ""
        elif match := _NODE_REF.match(line):
            ref = match.group(1)
        elif match := _NODE_PIN.match(line):
            if net and ref:
                out[(ref, match.group(1))] = net
    return out


# --------------------------------------------------------------------------- #
# The acceptance criterion: identical to KiCad, entry for entry
# --------------------------------------------------------------------------- #


def test_pin_nets_match_kicad_entry_for_entry() -> None:
    sheet = _root_sheet(_DEMO)
    expected = _nodes_by_indentation(export_netlist(sheet))
    view = _ConnectivityView.from_schematic(sheet)
    assert len(expected) == 236, "corpus changed; re-derive the numbers below"
    assert view.pin_nets == expected


def test_r15_pin1_is_its_own_net_not_ground() -> None:
    """The single entry that exposed the old path.

    ``SchematicGraph`` placed ``R15:1`` on ``GND``; KiCad puts it on an unnamed
    net of its own. Merging the two is what produced the false shorts, so this
    stays as a named regression rather than only inside the bulk comparison.
    """
    view = _ConnectivityView.from_schematic(_root_sheet(_DEMO))
    assert view.pin_nets[("R15", "1")] == "Net-(R15-Pad1)"
    assert "Net-(R15-Pad1)" not in view.ground_nets


# --------------------------------------------------------------------------- #
# Hierarchy: the reason components come from the export too
# --------------------------------------------------------------------------- #


def test_child_sheet_components_are_present() -> None:
    view = _ConnectivityView.from_schematic(_root_sheet(_DEMO))
    # These live on pic_sockets.kicad_sch, not on the root sheet.
    assert {"C6", "C7", "P2", "P3", "U1", "U5", "U6"} <= set(view.parts)
    assert len(view.parts) == 63


def test_a_single_sheet_parse_would_have_missed_them() -> None:
    """Pins the netlist attributes to child-sheet parts would dangle.

    This is why ``parts`` is not read from ``SchematicDoc.components()``:
    ``pin_nets`` would then reference components ``parts`` does not contain, and
    every check that looks a part up would silently find nothing.
    """
    from ratsnestpro.eda.adapter import SchematicDoc

    root_only = {
        str(c.get("reference") or "") for c in SchematicDoc.load(_root_sheet(_DEMO)).components()
    }
    assert "U5" not in root_only
    assert "U6" not in root_only


def test_power_flags_are_not_components() -> None:
    """KiCad resolves ``#PWR`` / ``#FLG`` into net names, so none reach ``parts``."""
    view = _ConnectivityView.from_schematic(_root_sheet(_DEMO))
    assert not [ref for ref in view.parts if ref.startswith("#")]
    assert "GND" in view.ground_nets


def test_every_pin_net_names_a_known_part() -> None:
    view = _ConnectivityView.from_schematic(_root_sheet(_DEMO))
    assert not {ref for ref, _pin in view.pin_nets} - set(view.parts)


# --------------------------------------------------------------------------- #
# no-connect, which a file-only reading could not claim
# --------------------------------------------------------------------------- #


def test_no_connect_matches_the_markers_in_the_sheets() -> None:
    """Counted independently: markers in the files vs. what the view claims."""
    from ratsnestpro.eda.vendor.sexpr import find_all, loads

    sheet = _root_sheet(_DEMO)
    in_files = sum(
        len(find_all(loads(p.read_text(encoding="utf-8")), "no_connect"))
        for p in sorted(sheet.parent.glob("*.kicad_sch"))
    )
    view = _ConnectivityView.from_schematic(sheet)
    assert in_files == 77
    assert len(view.no_connect) == in_files


def test_pins_are_populated_for_electrical_parts() -> None:
    view = _ConnectivityView.from_schematic(_root_sheet(_DEMO))
    without = {ref for ref, pins in view.pins.items() if not pins}
    # Only the six mounting holes have no pins; their libpart declares none.
    assert without == {"P101", "P102", "P103", "P104", "P105", "P106"}


# --------------------------------------------------------------------------- #
# Failure behaviour
# --------------------------------------------------------------------------- #


def test_absent_cli_raises_instead_of_yielding_an_empty_view(monkeypatch) -> None:
    """An empty view would make every connection check pass silently."""
    from ratsnestpro.eda import netlist as netlist_module
    from ratsnestpro.eda.vendor import kicad_cli

    def _missing(explicit: str | None = None) -> str:
        raise kicad_cli.KicadCliNotFound("stubbed away")

    monkeypatch.setattr(kicad_cli, "find_kicad_cli", _missing)
    # The loader memoises per (path, mtime, size), and earlier tests here have
    # already populated it, so a cached answer would arrive before the stub.
    netlist_module.clear_cache()
    with pytest.raises(NetlistError):
        _ConnectivityView.from_schematic(_root_sheet(_DEMO))


def test_missing_file_raises() -> None:
    with pytest.raises(NetlistError):
        _ConnectivityView.from_schematic("no-such-sheet.kicad_sch")


# --------------------------------------------------------------------------- #
# Corpus sweep: opt-in, ~110 s of kicad-cli processes
# --------------------------------------------------------------------------- #


@pytest.mark.real_kicad
def test_every_demo_project_exports_a_netlist() -> None:
    failures = demos.netlist_export_failures()
    assert not failures, f"netlist export failed for {[str(p) for p in failures]}"
    assert len(demos.demo_netlists()) >= 30


@pytest.mark.real_kicad
def test_corpus_has_one_known_two_terminal_short() -> None:
    """The false-positive floor for this check on known-good designs.

    The predecessor path reported 34 shorts here, all of them artefacts of
    merged nets. One finding survives, and it is a property of the upstream demo
    rather than of the check:

    ``royalblue54L_feather/nfc_antenna`` is a one-component board — a two-pin
    FPC connector with both pins on ``/ANT``. The antenna coil itself is copper
    on the PCB and is never drawn in the schematic, so the schematic alone does
    say the connector is bridged. Reported rather than suppressed: from the
    schematic's own evidence the loop is missing, and KiCad ERC does not look at
    this at all.

    Delete this expectation if the demo gains a symbol for its coil.
    """
    findings: list[str] = []
    for path, netlist in demos.demo_netlists():
        view = _ConnectivityView.from_schematic(path)
        assert view.pin_nets == dict(netlist.pin_nets)  # type: ignore[attr-defined]
        findings.extend(
            f"{path.parent.name}:{check.name}"
            for check in _two_terminal_short_checks(view)
            if not check.ok
        )
    assert findings == ["RoyalBlue54L-NFC-Antenna:two_terminal_not_shorted:J1"]


@pytest.mark.real_kicad
def test_corpus_yields_a_substantial_number_of_connections() -> None:
    """A view that builds but resolves nothing would pass every check above."""
    total = sum(len(nl.pin_nets) for _p, nl in demos.demo_netlists())  # type: ignore[attr-defined]
    assert total > 15000, f"only {total} pin->net entries across the corpus"



# --------------------------------------------------------------------------- #
# Ground-name recognition, which decides what every rail check compares against
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "GND",
        "GNDD",      # digital ground; BeagleBone-Black-Cape names its ground this
        "GNDA",
        "DGND",
        "AGND",
        "PGND",
        "EGND",
        "GNDPWR",
        "GND1",      # an index names one instance of a rail
        "VSS2",
        "VSS",
        "VSSA",
        "VEE",
        "EARTH",
        "GROUND",
        "/GNDD",     # hierarchical local label
        "+GND",
    ],
)
def test_ground_name_variants_are_recognised(name: str) -> None:
    """A domain prefix, suffix or index does not make a different net class.

    Recognising only the bare token left ``ground_nets`` empty on the official
    ``BeagleBone-Black-Cape`` template, and every ground pin on that board was
    then reported as not reaching ground.
    """
    from ratsnestpro.orchestration.pipeline import _looks_like_ground

    assert _looks_like_ground(name), name


@pytest.mark.parametrize(
    "name",
    ["GNDSENSE", "VDD33", "VBUS", "5V", "+3V3", "SIGNAL", "", "NET1"],
)
def test_non_ground_names_are_not_recognised(name: str) -> None:
    """``GNDSENSE`` measures ground; it is not the return path.

    This is why the match is on whole tokens with only a numeric suffix stripped,
    rather than on the substring ``GND``: a check that accepted a sense line as
    ground would pass a board whose ground pin never reaches ground.
    """
    from ratsnestpro.orchestration.pipeline import _looks_like_ground

    assert not _looks_like_ground(name), name
