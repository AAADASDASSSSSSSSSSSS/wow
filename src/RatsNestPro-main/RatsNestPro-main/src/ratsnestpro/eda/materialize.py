"""Materialize pipeline designs into real KiCad schematics.

The adaptive pipeline embeds library symbols, places labels on real pin
coordinates, emits explicit no-connect markers, and drives power nets with
``PWR_FLAG`` symbols so the resulting schematic can be checked by
``kicad-cli``.
"""

from __future__ import annotations

import re
from typing import Any

from ratsnestpro.domain.contracts import BoardPlan, CircuitIR
from ratsnestpro.eda import SchematicDoc
from ratsnestpro.eda import symbols as _symbols


def materialize_design(
    ir: CircuitIR,
    board: BoardPlan,
    supply_net: str = "3V3",
) -> SchematicDoc:
    """Build a pin-connected schematic from the IR and placement plan.

    Pipeline A used to place one free-floating label per logical pin on an
    arbitrary grid.  Its internal name/count round-trip passed, but KiCad quite
    correctly reported every label as dangling.  Use the same real-pin
    materializer as adaptive pipeline B so both entry points share one
    electrical truth source.
    """
    components = []
    # A board placement is not a schematic placement.  Reusing board-space
    # coordinates here put several symbols directly on top of one another and
    # could electrically short unrelated labelled pins.  Pipeline A has no
    # separate schematic-layout artifact, so use a deterministic, spacious
    # sheet grid instead.  ``board`` remains part of the stable API and is used
    # by the persisted design plan, just not as sheet geometry.
    _ = board
    for index, comp in enumerate(ir.components):
        column = index % 4
        row = index // 4
        components.append(
            {
                "ref": comp.ref,
                "symbol": comp.symbol,
                "value": comp.value,
                "footprint": comp.footprint,
                "x": 25.4 + column * 50.8,
                "y": 20.32 + row * 27.94,
                "rotation": 0.0,
            }
        )
    symbol_by_ref = {component.ref: component.symbol for component in ir.components}
    nets = []
    for net in ir.nets:
        mapped_pins = []
        for pin in net.pins:
            symbol = symbol_by_ref.get(pin.component_ref, "")
            number = _physical_pin_number(symbol, pin.pin) or pin.pin
            mapped_pins.append({"ref": pin.component_ref, "number": number})
        nets.append({"name": net.name, "pins": mapped_pins})
    no_connect_pins = [
        {
            "ref": pin.component_ref,
            "number": (
                _physical_pin_number(
                    symbol_by_ref.get(pin.component_ref, ""),
                    pin.pin,
                )
                or pin.pin
            ),
        }
        for pin in ir.no_connect_pins
    ]
    power_nets = [supply_net]
    for net in ir.nets:
        purpose = str(net.properties.get("purpose", "")).casefold()
        if net.name.upper() in {"VBUS", "VIN"} or "input" in purpose:
            if net.name not in power_nets:
                power_nets.append(net.name)
    return materialize_pinmapped(
        components,
        nets,
        no_connect_pins=no_connect_pins,
        supply_nets=power_nets,
        ground_net="GND",
    )


def _physical_pin_number(symbol: str, logical: str) -> str | None:
    """Resolve pipeline A's logical pin aliases against the real symbol."""
    pins = _symbols.symbol_pins(symbol) or []
    term = logical.strip().casefold()
    if not term:
        return None
    aliases = {term}
    if term in {"in", "out"}:
        aliases.add(f"v{term}")
    for pin in pins:
        if str(pin.get("number", "")).casefold() in aliases:
            return str(pin["number"])
    for pin in pins:
        names = {str(pin.get("name", "")).casefold()}
        names.update(str(value).casefold() for value in pin.get("alternates", ()))
        tokens = {
            token
            for name in names
            for token in re.split(r"[/~{}() ]+", name)
            if token
        }
        if aliases & (names | tokens):
            return str(pin["number"])
    return None



def materialize_pinmapped(
    components: list[dict[str, Any]],
    nets: list[dict[str, Any]],
    no_connect_pins: list[dict[str, Any]] | None = None,
    supply_nets: list[str] | None = None,
    ground_net: str = "GND",
) -> SchematicDoc:
    """Build a SchematicDoc from pipeline artifacts, embedding real pin geometry.

    ``components``: {ref, symbol, value, footprint, x, y, rotation}.
    ``nets``: {name, pins:[{ref, number}]}.

    Each net pin's label is placed at the *actual* pin coordinate — the
    component placement transformed by the symbol's real pin geometry
    (``symbols.symbol_pins`` + ``transform_pin``) — rather than an arbitrary
    grid. This makes the sheet geometrically faithful and lets the label
    netlist round-trip to the intended connectivity. When symbol geometry is
    unavailable, labels fall back to a per-net grid so name-based connectivity
    still round-trips.
    """
    supply_nets = supply_nets or []
    no_connect_pins = no_connect_pins or []
    doc = SchematicDoc.new()

    placements: dict[str, tuple[float, float, float]] = {}
    pins_cache: dict[str, list[dict[str, Any]] | None] = {}
    for c in components:
        ref = str(c["ref"])
        symbol = str(c["symbol"])
        x = _snap_schematic_coord(float(c.get("x", 20.0)))
        y = _snap_schematic_coord(float(c.get("y", 20.0)))
        rot = float(c.get("rotation", 0.0))
        placements[ref] = (x, y, rot)
        if symbol not in pins_cache:
            pins_cache[symbol] = _symbols.symbol_pins(symbol)
        doc.add_component(
            lib_id=symbol, reference=ref, value=str(c.get("value", "")),
            x=x, y=y, rotation=rot, footprint=str(c.get("footprint", "")),
        )

    ref_symbol = {str(c["ref"]): str(c["symbol"]) for c in components}
    fallback_x, fallback_y, step = 200.0, 10.0, 2.54
    counter = 0
    connected_pins: set[str] = set()
    first_net_coord: dict[str, tuple[float, float]] = {}
    power_output_nets: set[str] = set()
    for net in nets:
        name = str(net["name"])
        for pin in net.get("pins") or []:
            ref = str(pin["ref"])
            number = str(pin["number"])
            connected_pins.add(f"{ref}:{number}")
            coord = _pin_coord(ref, number, placements, ref_symbol, pins_cache)
            if coord is None:
                coord = (fallback_x + step * (counter % 40), fallback_y + step * (counter // 40))
                counter += 1
            doc.add_net_label(name, coord[0], coord[1])
            first_net_coord.setdefault(name, coord)
            symbol = ref_symbol.get(ref)
            if symbol is not None and any(
                str(candidate.get("number", "")) == number
                and str(candidate.get("type", "")).lower() == "power_out"
                for candidate in (pins_cache.get(symbol) or [])
            ):
                power_output_nets.add(name)

    for pin in no_connect_pins:
        ref = str(pin["ref"])
        number = str(pin["number"])
        if f"{ref}:{number}" in connected_pins:
            continue
        coord = _pin_coord(ref, number, placements, ref_symbol, pins_cache)
        if coord is not None:
            doc.add_no_connect(coord[0], coord[1])

    seen_power: set[str] = set()
    for name in [ground_net, *supply_nets]:
        if name and name not in seen_power and name not in power_output_nets:
            coord = first_net_coord.get(name)
            if coord is not None:
                # A PWR_FLAG placed on the already-labelled net gives KiCad ERC
                # a real power-output driver. The old isolated rail symbols
                # created dangling pins and false power-not-driven errors.
                doc.add_power_symbol("PWR_FLAG", coord[0], coord[1])
            seen_power.add(name)
    doc.embed_lib_symbols()
    return doc


def _snap_schematic_coord(value: float, grid: float = 1.27) -> float:
    """Snap symbol origins to KiCad's conventional 50 mil connection grid."""
    return round(value / grid) * grid


def _pin_coord(
    ref: str,
    number: str,
    placements: dict[str, tuple[float, float, float]],
    ref_symbol: dict[str, str],
    pins_cache: dict[str, list[dict[str, Any]] | None],
) -> tuple[float, float] | None:
    place = placements.get(ref)
    symbol = ref_symbol.get(ref)
    if place is None or symbol is None:
        return None
    pins = pins_cache.get(symbol)
    if not pins:
        return None
    for p in pins:
        if str(p["number"]) == number:
            px, py, rot = place
            return _symbols.transform_pin(px, py, rot, None, float(p["x"]), float(p["y"]))
    return None
