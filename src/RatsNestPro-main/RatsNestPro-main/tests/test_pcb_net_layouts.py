"""``PcbBoard.list_nets`` reads both of KiCad's board net layouts.

KiCad changed how a board records its nets between format ``20250907`` and
``20251101``:

old (``20250907`` and earlier)
    Every net is declared once at the top level as ``(net INDEX "NAME")`` and a
    pad references the index.
new (``20251101`` onward)
    That table is gone. A pad carries ``(net "NAME")`` directly, and the board's
    net list is whatever its pads name.

Both ship in KiCad 10's own demos — ``CM5_MINIMA_3`` is old, ``pic_programmer``
is new — so a reader that knows only one is wrong on real files half the time.

Why this file exists
--------------------
Reading only the old layout returned an empty net list for every board pcbnew 10
saves, and an empty list is indistinguishable from a board with no nets. That was
recorded as a defect in the pipeline's own output ("the generated .kicad_pcb is
missing its top-level net declarations") and was blamed for a Freerouting
failure. The output was correct KiCad 10 all along; the reader was three format
versions behind. The unit tests below need no KiCad install, so the next format
change surfaces here rather than as a silently empty netlist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.eda.vendor.sexpr import loads
from tests.fixtures import kicad_demos as demos

# One footprint, two pads, in each layout. Trimmed to what the reader looks at.
_OLD_LAYOUT = """\
(kicad_pcb
\t(version 20250907)
\t(generator "pcbnew")
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "VDD33")
\t(footprint "R_0603"
\t\t(pad "1" smd rect
\t\t\t(net 2 "VDD33")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(net 1 "GND")
\t\t)
\t)
\t(segment
\t\t(width 0.2)
\t\t(net 1)
\t)
)
"""

_NEW_LAYOUT = """\
(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(footprint "R_0603"
\t\t(pad "1" smd rect
\t\t\t(net "VDD33")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(net "GND")
\t\t)
\t)
\t(segment
\t\t(width 0.2)
\t\t(net 1)
\t)
)
"""


def _board(text: str) -> PcbBoard:
    return PcbBoard(loads(text))


def test_old_layout_reads_the_declaration_table() -> None:
    nets = _board(_OLD_LAYOUT).list_nets()
    assert nets == [
        {"index": 0, "name": ""},
        {"index": 1, "name": "GND"},
        {"index": 2, "name": "VDD33"},
    ]


def test_new_layout_reads_the_names_off_the_pads() -> None:
    nets = _board(_NEW_LAYOUT).list_nets()
    assert [n["name"] for n in nets] == ["GND", "VDD33"]


@pytest.mark.parametrize("text", [_OLD_LAYOUT, _NEW_LAYOUT])
def test_pad_net_names_reads_real_pad_assignments_in_both_layouts(text: str) -> None:
    assert _board(text).pad_net_names() == ["GND", "VDD33"]


def test_segment_net_is_not_mistaken_for_a_net_name() -> None:
    """``(net 1)`` on a segment is an index reference, not a declaration.

    It has the same two-token shape as a new-layout pad net, so the reader keys
    on where the node sits rather than on its length alone. Counting it would add
    a net called "1".
    """
    assert "1" not in {n["name"] for n in _board(_NEW_LAYOUT).list_nets()}


def test_board_with_no_nets_reads_as_no_nets() -> None:
    empty = '(kicad_pcb (version 20260206) (footprint "R_0603" (pad "1" smd rect)))'
    assert _board(empty).list_nets() == []


def test_unnamed_pad_net_is_skipped() -> None:
    """An empty name is the no-net marker, not a net called ""."""
    text = '(kicad_pcb (version 20260206) (footprint "F" (pad "1" smd rect (net ""))))'
    assert _board(text).list_nets() == []


# --------------------------------------------------------------------------- #
# Against the real files that motivated the fix
# --------------------------------------------------------------------------- #


@demos.requires_demos
@pytest.mark.parametrize(
    ("project", "board_name", "expected"),
    [
        # Old layout: a top-level table of 221.
        ("cm5_minima", "CM5_MINIMA_3.kicad_pcb", 221),
        # New layout: 111 nets, and no top-level table at all. The same number
        # kicad-cli's netlist reports for this project's schematic.
        ("pic_programmer", "pic_programmer.kicad_pcb", 111),
    ],
)
def test_shipped_demos_of_both_layouts_are_read(
    project: str, board_name: str, expected: int
) -> None:
    root = demos.demos_root()
    assert root is not None
    board = PcbBoard.load(root / project / board_name)
    assert len(board.list_nets()) == expected


@demos.requires_positive_sample
@demos.requires_kicad_cli
def test_pipeline_output_board_agrees_with_its_own_schematic() -> None:
    """The routed board's nets are exactly the schematic's named nets.

    This is the assertion that shows the output was never malformed. It compares
    two independent readers: KiCad's netlister on the schematic, and this parser
    on the board pcbnew saved.
    """
    from ratsnestpro.eda.netlist import netlist_for_schematic

    run = demos.positive_sample_run()
    assert run is not None
    netlist = netlist_for_schematic(run / "stm32f103c8t6-board.kicad_sch")
    from_schematic = {
        name.lstrip("/")
        for name in netlist.net_names
        if not name.startswith("unconnected-")
    }
    board = PcbBoard.load(run / "stm32f103c8t6-board.kicad_pcb")
    from_board = {
        n["name"].lstrip("/")
        for n in board.list_nets()
        if not n["name"].startswith("unconnected-")
    }
    assert from_board == from_schematic
    assert len(from_board) == 15


@demos.requires_positive_sample
def test_unrouted_baseline_still_reads_as_the_old_layout() -> None:
    """The pipeline's own writer emits format 20231120, which is the old layout.

    Before routing, pads carry no net at all — nets are assigned by the routing
    worker through pcbnew — so one declared net (index 0, unnamed) is the whole
    table and that is correct, not a defect.
    """
    run = demos.positive_sample_run()
    assert run is not None
    baseline = run / "stm32f103c8t6-board.unrouted.kicad_pcb"
    if not baseline.is_file():
        pytest.skip("unrouted baseline not present in this run")
    assert PcbBoard.load(baseline).list_nets() == [{"index": 0, "name": ""}]


@demos.requires_demos
def test_no_default_tier_demo_board_reads_as_netless_when_it_has_pad_nets() -> None:
    """A board whose pads name nets must never report an empty net list.

    The regression this guards is exactly the one that hid: a reader that knows
    one layout returns [] on the other, and [] reads as "this board has no nets".
    """
    offenders: list[str] = []
    for path in demos.demo_boards():
        board = PcbBoard.load(path)
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        if "(net " in text and not board.list_nets():
            offenders.append(path.name)
    assert not offenders, f"boards with net nodes but an empty net list: {offenders}"
