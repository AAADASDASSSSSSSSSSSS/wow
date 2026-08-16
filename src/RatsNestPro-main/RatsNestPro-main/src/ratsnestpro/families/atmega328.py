"""ATmega328 USB-C development-board family — parameterized (B-tier).

The reference circuit follows the RatsNest golden design; here it is turned
into a *template + parameters*. Different validated parameter sets produce
visibly different boards (crystal frequency, supply rail, decoupling count,
power LED, breakout headers, mounting holes) while every variation stays
inside a whitelist and is checked by the deterministic verifier.

Hard cross-parameter fact (ATmega328P speed grade): 16 MHz operation requires
a >= 4.5 V supply, so a 16 MHz crystal forces the 5.0 V rail. This is enforced
in the params contract — an illegal combination is rejected before any EDA
action.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ratsnestpro.domain.contracts import (
    BoardOutline,
    BoardPlan,
    CircuitIR,
    ComponentSpec,
    NetSpec,
    PinRef,
    PlacementSpec,
)
from ratsnestpro.verification.expectations import Expectations

FAMILY_ID = "atmega328-dev-board"

# ATmega328P-AU (TQFP-32) pin roles used by this template.
MCU_SUPPLY_PINS = ["4", "6", "18"]  # VCC / AVCC family
MCU_GND_PINS = ["3", "5", "21"]
MCU_XTAL1_PIN = "7"
MCU_XTAL2_PIN = "8"
MCU_RESET_PIN = "29"
# GPIO pins available to break out, in a stable order.
MCU_GPIO_PINS = ["30", "31", "32", "1", "2", "9", "10", "11", "12", "13", "14", "15"]

# TQFP-32 pins this family never uses: the remaining port bits and the ADC-only
# inputs. Enumerated because "unused" has to be said out loud — ERC treats a
# dangling pin with no no-connect marker as a defect, and it is right to: the
# family is the only place that knows these are open by design rather than by
# oversight. GPIO the breakout headers do not reach is added to this at build
# time, since that set depends on the parameters.
MCU_UNUSED_PINS = ["16", "17", "19", "20", "22", "23", "24", "25", "26", "27", "28"]

# Crystal load capacitor by frequency (illustrative but datasheet-plausible).
_LOAD_CAP = {8: "22pF", 16: "18pF"}

# LDO catalog by output voltage.
_LDO = {
    3.3: ("Regulator_Linear:AP2112K-3.3", "AP2112K-3.3", "AP2112K-3.3"),
    5.0: ("Regulator_Linear:AP2112K-5.0", "AP2112K-5.0", "AP2112K-5.0"),
}


class Atmega328Params(BaseModel):
    """Validated in-family parameters. Illegal or contradictory combinations
    are rejected here, before any schematic is materialized."""

    model_config = ConfigDict(extra="forbid")

    crystal_mhz: Literal[8, 16] = 16
    ldo_output_v: float = 5.0
    decoupling_count: int = Field(default=6, ge=4, le=8)
    power_led: bool = True
    breakout_rows: Literal[1, 2] = 2
    breakout_pins_per_row: int = Field(default=8, ge=4, le=12)
    mounting_holes: Literal[0, 4] = 4

    @model_validator(mode="after")
    def _cross_rules(self) -> Atmega328Params:
        # Only the qualified supply rails are allowed.
        if self.ldo_output_v not in (3.3, 5.0):
            raise ValueError("ldo_output_v must be 3.3 or 5.0")
        # ATmega328P: 16 MHz needs >= 4.5 V. Forbid 16 MHz on the 3.3 V rail.
        if self.crystal_mhz >= 16 and self.ldo_output_v < 4.5:
            raise ValueError(
                "16 MHz operation requires a 5.0 V supply on ATmega328P; "
                "either lower the crystal to 8 MHz or select the 5.0 V LDO"
            )
        # Enough GPIO to fill the breakout headers (each row uses 2 pins for
        # power/ground, the rest for signals).
        signal_pins = self.breakout_rows * (self.breakout_pins_per_row - 2)
        if signal_pins > len(MCU_GPIO_PINS):
            raise ValueError(
                f"requested {signal_pins} breakout signal pins but only "
                f"{len(MCU_GPIO_PINS)} MCU GPIO pins are available"
            )
        return self

    @property
    def load_cap(self) -> str:
        return _LOAD_CAP[self.crystal_mhz]

    @property
    def signal_pins(self) -> int:
        return self.breakout_rows * (self.breakout_pins_per_row - 2)


def expectations_for(params: Atmega328Params) -> Expectations:
    return Expectations(
        family=FAMILY_ID,
        supply_net="3V3",
        supply_voltage_v=params.ldo_output_v,
        gnd_net="GND",
        decoupling_count=params.decoupling_count,
        decoupling_value="100nF",
        crystal_freq_mhz=params.crystal_mhz,
        crystal_load_cap=params.load_cap,
        ldo_input_cap="1uF",
        ldo_output_cap="1uF",
        power_led=params.power_led,
        header_signal_pins=params.signal_pins,
        mounting_holes=params.mounting_holes,
    )


def _c(ref: str, value: str, role: str, **props: str) -> ComponentSpec:
    return ComponentSpec(
        ref=ref,
        symbol="Device:C",
        value=value,
        footprint="Capacitor_SMD:C_0603_1608Metric",
        catalog_id=f"CAP-{value}-0603",
        role=role,
        properties=props,
    )


def _r(ref: str, value: str, role: str) -> ComponentSpec:
    return ComponentSpec(
        ref=ref,
        symbol="Device:R",
        value=value,
        footprint="Resistor_SMD:R_0603_1608Metric",
        catalog_id=f"RES-{value}-0603",
        role=role,
    )


def build_ir(params: Atmega328Params | None = None) -> CircuitIR:
    """Build the reviewed circuit IR for the given parameters.

    The default parameters reproduce the golden reference board (16 MHz / 5 V,
    six decouplers, power LED, two 8-pin headers, four mounting holes)."""
    params = params or Atmega328Params()
    supply = "3V3"  # net name kept stable; its voltage carries the rail value
    components: list[ComponentSpec] = []
    nets: list[NetSpec] = []

    # --- power input: USB-C + CC pulldowns ---------------------------------
    components += [
        ComponentSpec(
            ref="J1",
            symbol="Connector:USB_C_Receptacle_PowerOnly_6P",
            value="USB-C power",
            footprint="Connector_USB:USB_C_Receptacle_GCT_USB4135-GF-A_6P_TopMnt_Horizontal",
            catalog_id="USB-C-POWER-ONLY",
            role="power_input",
            properties={"source_voltage_v": "5.0"},
        ),
        _r("R1", "5.1k", "usb_cc"),
        _r("R2", "5.1k", "usb_cc"),
    ]

    # --- LDO regulator (parameterized output) ------------------------------
    ldo_symbol, ldo_value, ldo_catalog = _LDO[params.ldo_output_v]
    components.append(
        ComponentSpec(
            ref="U1",
            symbol=ldo_symbol,
            value=ldo_value,
            footprint="Package_TO_SOT_SMD:SOT-23-5",
            catalog_id=ldo_catalog,
            role="ldo",
            properties={
                "input_voltage_v": "5.0",
                "output_voltage_v": str(params.ldo_output_v),
            },
        )
    )
    components += [
        _c("C9", "1uF", "ldo_input"),
        _c("C10", "1uF", "ldo_output"),
    ]

    # --- MCU ---------------------------------------------------------------
    components.append(
        ComponentSpec(
            ref="U2",
            symbol="MCU_Microchip_ATmega:ATmega328P-A",
            value="ATmega328P-AU",
            footprint="Package_QFP:TQFP-32_7x7mm_P0.8mm",
            catalog_id="ATMEGA328P-AU",
            role="mcu",
            properties={
                "supply_voltage_v": str(params.ldo_output_v),
                "package": "TQFP-32",
            },
        )
    )

    # --- crystal + load caps (parameterized frequency) ---------------------
    components += [
        ComponentSpec(
            ref="Y1",
            symbol="Device:Crystal_GND24",
            value=f"{params.crystal_mhz}MHz",
            footprint="Crystal:Crystal_SMD_3225-4Pin_3.2x2.5mm",
            catalog_id=f"CRYSTAL-{params.crystal_mhz}MHZ",
            role="crystal",
        ),
        _c("C1", params.load_cap, "crystal_load"),
        _c("C2", params.load_cap, "crystal_load"),
    ]

    # --- decoupling caps (parameterized count) -----------------------------
    decoupling_refs = [f"C{i}" for i in range(3, 3 + params.decoupling_count)]
    for ref in decoupling_refs:
        components.append(_c(ref, "100nF", "decoupling", rail=supply, decoupling="true"))

    # --- reset ------------------------------------------------------------
    components += [
        _r("R3", "10k", "reset_pullup"),
        ComponentSpec(
            ref="SW1",
            symbol="Switch:SW_Push",
            value="RESET",
            footprint="Button_Switch_SMD:SW_SPST_B3U-1000P",
            catalog_id="SW-PUSH-TACTILE",
            role="reset_button",
        ),
    ]

    # --- optional power LED ------------------------------------------------
    if params.power_led:
        components += [
            ComponentSpec(
                ref="D1",
                symbol="Device:LED",
                value="GREEN",
                footprint="LED_SMD:LED_0603_1608Metric",
                catalog_id="LED-GREEN-0603",
                role="power_led",
            ),
            _r("R4", "1k", "led_resistor"),
        ]

    # --- breakout headers --------------------------------------------------
    header_refs = ["J2", "J3"][: params.breakout_rows]
    ppr = params.breakout_pins_per_row
    for ref in header_refs:
        components.append(
            ComponentSpec(
                ref=ref,
                symbol=f"Connector_Generic:Conn_01x{ppr:02d}",
                value=f"1x{ppr:02d} header",
                footprint=f"Connector_PinHeader_2.54mm:PinHeader_1x{ppr:02d}_P2.54mm_Vertical",
                catalog_id=f"HEADER-1X{ppr:02d}",
                role="breakout_header",
            )
        )

    # --- mounting holes ----------------------------------------------------
    if params.mounting_holes:
        for i in range(1, params.mounting_holes + 1):
            components.append(
                ComponentSpec(
                    ref=f"H{i}",
                    symbol="Mechanical:MountingHole",
                    value="M3 mounting hole",
                    footprint="MountingHole:MountingHole_3.2mm_M3",
                    catalog_id="MOUNTING-HOLE-M3",
                    role="mounting_hole",
                    in_bom=False,
                )
            )

    # ----------------------------------------------------------------------
    # Nets
    # ----------------------------------------------------------------------
    def pins(*items: tuple[str, str]) -> list[PinRef]:
        return [PinRef(component_ref=r, pin=p) for r, p in items]

    # VBUS: USB in → LDO in/enable → input cap. Both A9 and B9 are listed: a
    # USB-C receptacle is symmetric, and a cable inserted the other way up
    # delivers power on the row that was left floating.
    nets.append(
        NetSpec(
            name="VBUS",
            pins=pins(
                ("J1", "A9"), ("J1", "B9"),
                ("U1", "IN"), ("U1", "EN"), ("C9", "1"),
            ),
            properties={"voltage_v": "5.0", "purpose": "USB input and LDO enable"},
        )
    )
    nets.append(NetSpec(name="CC1", pins=pins(("J1", "A5"), ("R1", "1"))))
    nets.append(NetSpec(name="CC2", pins=pins(("J1", "B5"), ("R2", "1"))))

    # Supply rail (3V3 net name; voltage set by LDO output param)
    supply_pins: list[tuple[str, str]] = [("U1", "OUT"), ("C10", "1")]
    supply_pins += [("U2", p) for p in MCU_SUPPLY_PINS]
    supply_pins += [(ref, "1") for ref in decoupling_refs]
    supply_pins += [("R3", "1")]
    if params.power_led:
        supply_pins += [("R4", "1")]
    supply_pins += [(ref, "1") for ref in header_refs]  # header pin 1 = supply
    nets.append(
        NetSpec(
            name=supply,
            pins=pins(*supply_pins),
            properties={"voltage_v": str(params.ldo_output_v), "purpose": "regulated supply"},
        )
    )

    # Ground. Both receptacle ground rows and the shell are bonded: the B row for
    # the same reversibility reason as VBUS, the shell because an unbonded shield
    # is an antenna rather than a shield.
    gnd_pins: list[tuple[str, str]] = [
        ("J1", "A12"), ("J1", "B12"), ("J1", "SH"),
        ("R1", "2"), ("R2", "2"), ("U1", "GND"),
        ("C9", "2"), ("C10", "2"),
    ]
    gnd_pins += [("U2", p) for p in MCU_GND_PINS]
    # C1/C2 return, plus the crystal can's two shield pads.
    gnd_pins += [("C1", "2"), ("C2", "2"), ("Y1", "2"), ("Y1", "4")]
    gnd_pins += [(ref, "2") for ref in decoupling_refs]
    gnd_pins += [("SW1", "2")]
    if params.power_led:
        gnd_pins += [("D1", "K")]
    gnd_pins += [(ref, str(ppr)) for ref in header_refs]  # header last pin = GND
    nets.append(
        NetSpec(
            name="GND",
            pins=pins(*gnd_pins),
            properties={"voltage_v": "0.0", "purpose": "reference plane"},
        )
    )

    # Oscillator. ``Device:Crystal_GND24`` numbers its terminals 1 and 3; pins 2
    # and 4 are the can's grounded shield. The footprint has always been the
    # 4-pad 3225, so declaring the 2-pin ``Device:Crystal`` here left the shield
    # unconnected and — worse — put the second terminal on pin 2, which is a
    # ground pad, not a terminal.
    nets.append(NetSpec(name="XTAL1", pins=pins(("U2", MCU_XTAL1_PIN), ("Y1", "1"), ("C1", "1"))))
    nets.append(NetSpec(name="XTAL2", pins=pins(("U2", MCU_XTAL2_PIN), ("Y1", "3"), ("C2", "1"))))

    # Reset
    nets.append(
        NetSpec(name="RESET", pins=pins(("U2", MCU_RESET_PIN), ("R3", "2"), ("SW1", "1")))
    )

    # Power LED anode
    if params.power_led:
        nets.append(NetSpec(name="POWER_LED_A", pins=pins(("R4", "2"), ("D1", "A"))))

    # GPIO → breakout header signal pins (pins 2..ppr-1 on each header)
    gpio_iter = iter(MCU_GPIO_PINS)
    gpio_index = 0
    for ref in header_refs:
        for pin_no in range(2, ppr):  # signal pins
            mcu_pin = next(gpio_iter)
            nets.append(
                NetSpec(
                    name=f"GPIO_{gpio_index}",
                    pins=pins(("U2", mcu_pin), (ref, str(pin_no))),
                    properties={"purpose": "breakout"},
                )
            )
            gpio_index += 1

    # Pins left open: the ones this family never uses, plus any GPIO the chosen
    # header count does not reach. Listing them is the difference between "unused
    # by decision" and "forgotten" — the parameters shrink the mapped set without
    # saying anything about the remainder, and an unmarked dangling pin is an ERC
    # error.
    mcu_wired = {
        pin.pin for net in nets for pin in net.pins if pin.component_ref == "U2"
    }
    no_connect = [
        PinRef(component_ref="U2", pin=pin)
        for pin in [*MCU_GPIO_PINS, *MCU_UNUSED_PINS]
        if pin not in mcu_wired
    ]

    return CircuitIR(
        family=FAMILY_ID,
        components=components,
        nets=nets,
        no_connect_pins=no_connect,
        constraints=[
            "CC1 and CC2 each use an independent 5.1 kOhm pull-down",
            "MCU supply pins on the regulated rail; all MCU GND pins on GND",
            f"exactly {params.decoupling_count} 100 nF decouplers between rail and GND",
            "the crystal loop is short and contains no vias",
        ],
        rationale=(
            f"USB-C power feeds a {params.ldo_output_v} V LDO. The TQFP-32 MCU uses a "
            f"{params.crystal_mhz} MHz crystal (load {params.load_cap}). "
            f"{params.decoupling_count} local bypass capacitors are distinguished from "
            "regulator and oscillator capacitors by role."
        ),
    )


def build_plan(params: Atmega328Params | None = None) -> BoardPlan:
    """A simple, deterministic placement. Positions are illustrative; the
    schematic slice does not depend on exact coordinates."""
    params = params or Atmega328Params()
    ir = build_ir(params)
    # Lay components out on a coarse grid by role band.
    bands = {
        "power_input": 8.0,
        "usb_cc": 14.0,
        "ldo": 20.0,
        "ldo_input": 20.0,
        "ldo_output": 20.0,
        "mcu": 35.0,
        "crystal": 30.0,
        "crystal_load": 30.0,
        "decoupling": 40.0,
        "reset_pullup": 50.0,
        "reset_button": 50.0,
        "power_led": 55.0,
        "led_resistor": 55.0,
        "breakout_header": 25.0,
        "mounting_hole": 4.0,
    }
    placements: list[PlacementSpec] = []
    counters: dict[str, int] = {}
    corners = [(4.0, 4.0), (66.0, 4.0), (4.0, 46.0), (66.0, 46.0)]
    for comp in ir.components:
        if comp.role == "mounting_hole":
            idx = counters.get("mounting_hole", 0)
            x, y = corners[idx % 4]
            counters["mounting_hole"] = idx + 1
        else:
            x = bands.get(comp.role, 60.0)
            n = counters.get(comp.role, 0)
            y = 10.0 + n * 4.0
            counters[comp.role] = n + 1
        placements.append(PlacementSpec(ref=comp.ref, x_mm=x, y_mm=y))

    return BoardPlan(
        outline=BoardOutline(width_mm=70, height_mm=50),
        placements=placements,
        reference_plane_net="GND",
        copper_layers=["F.Cu", "B.Cu"],
        constraints=[
            "keep every 100 nF decoupler within 3 mm of its MCU supply pin",
            "keep the crystal and load caps within 5 mm of the oscillator pins",
            "route the crystal loop without vias",
            "fill GND zones on F.Cu and B.Cu",
        ],
        rationale="Connector→regulator→MCU flow with symmetric breakout headers.",
    )
