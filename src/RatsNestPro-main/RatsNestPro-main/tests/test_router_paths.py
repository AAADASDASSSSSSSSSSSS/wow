"""Freerouting cannot be handed a path containing a space, and tracks reference
their net in two different ways.

Both were found by re-diagnosing one failure. The recorded cause of that failure
("the generated .kicad_pcb is missing its top-level net declarations") was wrong
twice over: the board was valid KiCad 10, and the router had never even opened
it.

The real cause, from Freerouting v2.2.4's own log:

    WARN Unknown file type in -de argument: C:\\Users\\...\\OneDrive
    WARN Unknown command line argument: -
    WARN Unknown command line argument: Ericsson\\Desktop\\...\\board.dsn

It re-splits its own arguments on whitespace rather than using the argv the OS
handed it, so a checkout under "OneDrive - Ericsson" truncated the path at the
first space and the run died in ``RoutingJob.setInput`` ->
``FileInputStream.open``. Quoting cannot help: the OS-level argument was already
intact. Same DSN under a space-free directory routes normally.

Fixing that exposed the second half of the board-format migration: a segment
carries ``(net 1)`` before format 20251101 and ``(net "GND")`` after, and reading
the latter as an integer raised ``ValueError`` on every routed board KiCad 10
saves.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.eda.vendor.sexpr import loads


def _worker_module():
    """Load the routing worker with a stubbed ``pcbnew``.

    The worker targets KiCad's own interpreter and is never imported by the
    pipeline; ``_spacefree_dir`` is pure path logic, and testing it is worth the
    stub.
    """
    import ratsnestpro.eda as eda_pkg

    path = Path(eda_pkg.__file__).with_name("_route_worker.py")
    if not path.is_file():
        pytest.skip("routing worker not on disk")
    sys.modules.setdefault("pcbnew", types.ModuleType("pcbnew"))
    spec = importlib.util.spec_from_file_location("rnp_route_worker_under_test", path)
    if spec is None or spec.loader is None:
        pytest.skip("routing worker not loadable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# The router's working directory
# --------------------------------------------------------------------------- #


def test_space_free_directory_is_used_as_is() -> None:
    worker = _worker_module()
    assert worker._spacefree_dir(r"C:\work\run1") == r"C:\work\run1"


def test_directory_with_a_space_is_relocated(tmp_path: Path) -> None:
    """A relocation is the only fix; the argument is already correct at the OS
    level when Freerouting splits it again."""
    worker = _worker_module()
    spaced = str(tmp_path / "OneDrive - Ericsson" / "run")
    relocated = worker._spacefree_dir(spaced)
    assert relocated != spaced
    assert " " not in relocated
    assert Path(relocated).is_dir()


def test_relocation_target_is_actually_writable() -> None:
    """A directory that cannot hold the DSN would only move the failure."""
    worker = _worker_module()
    relocated = worker._spacefree_dir(r"C:\Users\some one\run")
    if " " in relocated:
        pytest.skip("no space-free temporary location on this machine")
    probe = Path(relocated) / "probe.dsn"
    probe.write_text("x", encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == "x"


# --------------------------------------------------------------------------- #
# How a track names its net
# --------------------------------------------------------------------------- #

_OLD_ROUTED = """\
(kicad_pcb
\t(version 20250907)
\t(net 0 "")
\t(net 1 "GND")
\t(net 2 "VDD33")
\t(segment
\t\t(start 10 10)
\t\t(end 20 10)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net 1)
\t\t(uuid "a")
\t)
\t(segment
\t\t(start 20 10)
\t\t(end 30 10)
\t\t(width 0.25)
\t\t(layer "B.Cu")
\t\t(net 2)
\t\t(uuid "b")
\t)
)
"""

_NEW_ROUTED = """\
(kicad_pcb
\t(version 20260206)
\t(footprint "R"
\t\t(pad "1" smd rect
\t\t\t(net "GND")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(net "VDD33")
\t\t)
\t)
\t(segment
\t\t(start 10 10)
\t\t(end 20 10)
\t\t(width 0.25)
\t\t(layer "F.Cu")
\t\t(net "GND")
\t\t(uuid "a")
\t)
\t(segment
\t\t(start 20 10)
\t\t(end 30 10)
\t\t(width 0.25)
\t\t(layer "B.Cu")
\t\t(net "VDD33")
\t\t(uuid "b")
\t)
)
"""


@pytest.mark.parametrize("text", [_OLD_ROUTED, _NEW_ROUTED], ids=["old", "new"])
def test_tracks_report_their_net_by_name_in_both_layouts(text: str) -> None:
    """The index is resolved through the table; the name is taken as written."""
    tracks = PcbBoard(loads(text)).list_tracks()
    assert [t["net"] for t in tracks] == ["GND", "VDD33"]
    assert [t["layer"] for t in tracks] == ["F.Cu", "B.Cu"]


@pytest.mark.parametrize("text", [_OLD_ROUTED, _NEW_ROUTED], ids=["old", "new"])
def test_filtering_by_net_name_works_in_both_layouts(text: str) -> None:
    board = PcbBoard(loads(text))
    assert len(board.list_tracks(net="GND")) == 1
    assert len(board.list_tracks(net="VDD33")) == 1
    assert board.list_tracks(net="NO_SUCH_NET") == []


def test_old_layout_still_accepts_an_index_filter() -> None:
    """Callers that knew the index keep working."""
    assert len(PcbBoard(loads(_OLD_ROUTED)).list_tracks(net=1)) == 1


def test_reading_a_new_layout_track_does_not_raise() -> None:
    """The regression: ``int("GND")`` on every routed board pcbnew 10 saves."""
    PcbBoard(loads(_NEW_ROUTED)).list_tracks()


def test_unresolvable_index_reports_no_net_rather_than_guessing() -> None:
    text = (
        '(kicad_pcb (version 20250907) (net 0 "") '
        '(segment (width 0.2) (layer "F.Cu") (net 7) (uuid "z")))'
    )
    assert PcbBoard(loads(text)).list_tracks()[0]["net"] is None
