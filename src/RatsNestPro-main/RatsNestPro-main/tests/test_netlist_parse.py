"""Parsing rules for ``kicad-cli sch export netlist`` output.

These run without KiCad installed: the sample below is a reduced copy of real
``--format kicadsexpr`` output, so the shapes it exercises are the shapes the
exporter actually emits rather than shapes invented for the test.

The export-and-compare side lives in ``test_connectivity_view_netlist.py`` and
needs a real install.
"""

from __future__ import annotations

import pytest

from ratsnestpro.eda.netlist import NetlistError, parse_netlist

# Two resistors sharing one libpart, a 3-pin regulator, and a mounting hole with
# no pins at all. R1/R2 are wired to the same net on one side, R2's second pin
# carries a no-connect marker, and one net name is a hierarchical local label.
SAMPLE = """\
(export
\t(version "E")
\t(design
\t\t(source "/tmp/demo.kicad_sch")
\t)
\t(components
\t\t(comp
\t\t\t(ref "R1")
\t\t\t(value "10k")
\t\t\t(footprint "Resistor_SMD:R_0603_1608Metric")
\t\t\t(libsource
\t\t\t\t(lib "Device")
\t\t\t\t(part "R")
\t\t\t)
\t\t)
\t\t(comp
\t\t\t(ref "R2")
\t\t\t(value "4k7")
\t\t\t(footprint "Resistor_SMD:R_0603_1608Metric")
\t\t\t(libsource
\t\t\t\t(lib "Device")
\t\t\t\t(part "R")
\t\t\t)
\t\t)
\t\t(comp
\t\t\t(ref "U1")
\t\t\t(value "AMS1117-3.3")
\t\t\t(footprint "Package_TO_SOT_SMD:SOT-223-3_TabPin2")
\t\t\t(libsource
\t\t\t\t(lib "Regulator_Linear")
\t\t\t\t(part "AMS1117-3.3")
\t\t\t)
\t\t)
\t\t(comp
\t\t\t(ref "H1")
\t\t\t(value "MountingHole")
\t\t\t(footprint "MountingHole:MountingHole_2.7mm")
\t\t\t(libsource
\t\t\t\t(lib "Mechanical")
\t\t\t\t(part "MountingHole")
\t\t\t)
\t\t)
\t)
\t(libparts
\t\t(libpart
\t\t\t(lib "Device")
\t\t\t(part "R")
\t\t\t(pins
\t\t\t\t(pin
\t\t\t\t\t(num "1")
\t\t\t\t\t(name "")
\t\t\t\t\t(type "passive")
\t\t\t\t)
\t\t\t\t(pin
\t\t\t\t\t(num "2")
\t\t\t\t\t(name "")
\t\t\t\t\t(type "passive")
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(libpart
\t\t\t(lib "Regulator_Linear")
\t\t\t(part "AMS1117-3.3")
\t\t\t(pins
\t\t\t\t(pin
\t\t\t\t\t(num "1")
\t\t\t\t\t(name "GND")
\t\t\t\t\t(type "power_in")
\t\t\t\t)
\t\t\t\t(pin
\t\t\t\t\t(num "2")
\t\t\t\t\t(name "VO")
\t\t\t\t\t(type "power_out")
\t\t\t\t)
\t\t\t\t(pin
\t\t\t\t\t(num "3")
\t\t\t\t\t(name "VI")
\t\t\t\t\t(type "power_in")
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(libpart
\t\t\t(lib "Mechanical")
\t\t\t(part "MountingHole")
\t\t)
\t)
\t(nets
\t\t(net
\t\t\t(code "1")
\t\t\t(name "GND")
\t\t\t(node
\t\t\t\t(ref "U1")
\t\t\t\t(pin "1")
\t\t\t\t(pinfunction "GND")
\t\t\t\t(pintype "power_in")
\t\t\t)
\t\t\t(node
\t\t\t\t(ref "R1")
\t\t\t\t(pin "2")
\t\t\t\t(pintype "passive")
\t\t\t)
\t\t)
\t\t(net
\t\t\t(code "2")
\t\t\t(name "/sub/VCC_IN")
\t\t\t(node
\t\t\t\t(ref "U1")
\t\t\t\t(pin "3")
\t\t\t\t(pinfunction "VI")
\t\t\t\t(pintype "power_in")
\t\t\t)
\t\t\t(node
\t\t\t\t(ref "R1")
\t\t\t\t(pin "1")
\t\t\t\t(pintype "passive")
\t\t\t)
\t\t\t(node
\t\t\t\t(ref "R2")
\t\t\t\t(pin "1")
\t\t\t\t(pintype "passive")
\t\t\t)
\t\t)
\t\t(net
\t\t\t(code "3")
\t\t\t(name "Net-(R2-Pad2)")
\t\t\t(node
\t\t\t\t(ref "R2")
\t\t\t\t(pin "2")
\t\t\t\t(pintype "passive+no_connect")
\t\t\t)
\t\t)
\t)
)
"""


def test_pin_nets_are_read_verbatim() -> None:
    netlist = parse_netlist(SAMPLE)
    assert netlist.pin_nets == {
        ("U1", "1"): "GND",
        ("R1", "2"): "GND",
        ("U1", "3"): "/sub/VCC_IN",
        ("R1", "1"): "/sub/VCC_IN",
        ("R2", "1"): "/sub/VCC_IN",
        ("R2", "2"): "Net-(R2-Pad2)",
    }


def test_hierarchical_net_name_keeps_its_path() -> None:
    """The exporter is the truth source; renaming its nets makes it something else."""
    netlist = parse_netlist(SAMPLE)
    assert netlist.pin_nets[("U1", "3")] == "/sub/VCC_IN"
    assert "/sub/VCC_IN" in netlist.net_names


def test_lib_id_is_recomposed_from_libsource() -> None:
    """The netlist splits what a schematic stores as one ``lib_id``."""
    netlist = parse_netlist(SAMPLE)
    assert netlist.components["R1"].lib_id == "Device:R"
    assert netlist.components["U1"].lib_id == "Regulator_Linear:AMS1117-3.3"


def test_component_fields_survive() -> None:
    netlist = parse_netlist(SAMPLE)
    comp = netlist.components["R2"]
    assert (comp.ref, comp.value) == ("R2", "4k7")
    assert comp.footprint == "Resistor_SMD:R_0603_1608Metric"


def test_one_libpart_supplies_pins_to_every_component_using_it() -> None:
    netlist = parse_netlist(SAMPLE)
    assert netlist.pins["R1"] == netlist.pins["R2"]
    assert [p["number"] for p in netlist.pins["R1"]] == ["1", "2"]
    assert [p["name"] for p in netlist.pins["U1"]] == ["GND", "VO", "VI"]
    assert [p["type"] for p in netlist.pins["U1"]] == [
        "power_in",
        "power_out",
        "power_in",
    ]


def test_pinless_libpart_yields_an_empty_pin_list_not_a_missing_key() -> None:
    """A mounting hole has no pins. Absent and empty must not be confused: a
    check that reads ``pins[ref]`` should see "no pins", not raise."""
    netlist = parse_netlist(SAMPLE)
    assert netlist.pins["H1"] == []
    assert "H1" in netlist.components


def test_no_connect_comes_from_the_pintype_suffix() -> None:
    """KiCad attributes the coordinate marker to a pin; we only read its verdict."""
    netlist = parse_netlist(SAMPLE)
    assert netlist.no_connect == {("R2", "2")}


def test_plain_pintypes_are_not_read_as_no_connect() -> None:
    netlist = parse_netlist(SAMPLE)
    assert ("U1", "1") not in netlist.no_connect
    assert ("R1", "2") not in netlist.no_connect


def test_every_pin_net_reference_exists_as_a_component() -> None:
    """The invariant that motivated taking all three sections from one export."""
    netlist = parse_netlist(SAMPLE)
    assert not {ref for ref, _pin in netlist.pin_nets} - set(netlist.components)


def test_non_netlist_input_is_rejected() -> None:
    with pytest.raises(NetlistError):
        parse_netlist('(kicad_sch (version "20231120"))')


def test_garbage_input_is_rejected() -> None:
    with pytest.raises(Exception):  # noqa: B017 - sexpr or NetlistError, both fine
        parse_netlist("not an s-expression at all")


def test_missing_sections_do_not_crash() -> None:
    """A netlist for an empty sheet has no components and no nets."""
    netlist = parse_netlist('(export (version "E"))')
    assert netlist.components == {}
    assert netlist.pin_nets == {}
    assert netlist.no_connect == set()
    assert netlist.net_names == set()


def test_unnamed_net_is_skipped_rather_than_given_a_made_up_name() -> None:
    text = (
        '(export (version "E") (nets (net (code "1") (name)'
        ' (node (ref "R1") (pin "1") (pintype "passive")))))'
    )
    assert parse_netlist(text).pin_nets == {}
