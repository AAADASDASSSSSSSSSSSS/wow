---
role: selection,schematic
title: RP2040 power, clock and flash
---

# RP2040 — power, clock & flash

Guidance specific to the Raspberry Pi RP2040 (56-pin 7×7 mm QFN, 0.4 mm pitch).

## Supplies
- RP2040 needs two rails: **3.3 V** (I/O) and **1.1 V** (digital core). An **internal LDO** converts 3.3 V → 1.1 V, so only 3.3 V must be supplied externally.
- Typical input: 5 V USB VBUS → an **external 3.3 V LDO** (the doc uses NCP1117, up to ~1 A). Follow the external LDO's datasheet (NCP1117 needs a **10 µF** cap on both input and output).

## Decoupling
- **100 nF per power pin**, placed close to the pin. (Sharing one cap between two pins is a space-saving trade-off that raises inductance and can limit max speed.)
- Internal regulator: place **1 µF** ceramic caps close to **VREG_IN** and **VREG_OUT** (low-ESR; small ceramics meet the requirement). Connect VREG_OUT to the DVDD pins.

## Clock (12 MHz crystal)
- Use a **12 MHz** crystal between XIN/XOUT (or feed a CMOS clock into XIN). Recommended part: Abracon **ABM8-272-T3** (±30 ppm, ESR ≤ 50 Ω, **CL = 10 pF**).
- Load caps: two equal caps to ground (series combination = C/2); add ~3 pF PCB/pin parasitic. Example: 2×**15 pF** → 7.5 + 3 ≈ 10.5 pF ≈ target 10 pF.
- Add a **1 kΩ series resistor** (R5) on the crystal to prevent over-driving at IOVDD = 3.3 V (re-tune for other IOVDD). Keep crystal traces short.

## QSPI flash + BOOTSEL
- Boot code lives in an external **quad-SPI flash** (e.g. W25Q128JV). Wire QSPI directly with **short** traces (signal integrity / crosstalk).
- **QSPI_SS**: optional **10 kΩ pull-up to 3.3 V** (needed for some flash to power up correctly; DNF for the W25Q here), plus a **1 kΩ** resistor to a **USB_BOOT** header — QSPI_SS is the BOOTSEL strap (logic 0 at boot → RP2040 enumerates as USB mass storage). Place these resistors close to the flash.

## Source
Hardware design with RP2040 (Raspberry Pi) — §2.1 Power, §2.2 Flash storage, §2.3 Crystal oscillator.
URL: https://datasheets.raspberrypi.com/rp2040/hardware-design-with-rp2040.pdf · accessed 2026-07-24.
