---
role: schematic
title: ESP32 clock, power-up/reset and strapping pins
---

# ESP32 — clock, power-up/reset & strapping

Guidance specific to the Espressif ESP32 SoC (scope: ESP32 series).

## Main crystal (compulsory)
- ESP32 firmware supports **only a 40 MHz** main crystal.
- Crystal accuracy should be within **±10 ppm**.
- Load caps C1, C2 from `CL = C1 × C2 / (C1 + C2 + Cstray)`, where CL is the crystal's load capacitance and Cstray is PCB stray capacitance; C1 and C2 are usually equal and tuned so the measured frequency offset is within ±10 ppm.
- Put a **series inductor on the XTAL_P** trace (start with 0 Ω, adjust after RF testing) to reduce harmonic impact on RF.
- Recommended crystal amplitude **> 500 mV**.

## Optional RTC crystal
- External **32.768 kHz** RTC crystal: ESR **≤ 70 kΩ**; bias parallel resistor **5 MΩ < R ≤ 10 MΩ**. If unused, the pins can be GPIOs.

## Power-up / reset (CHIP_PU)
- `CHIP_PU` high = enable, low = reset. It **must not be left floating**.
- Add an **RC delay** at `CHIP_PU`: recommended **R = 10 kΩ, C = 1 µF** (adjust to the actual supply).
- Timing minimums: `tSTBL` ≥ **50 µs** (rails stable before CHIP_PU is pulled high), `tRST` ≥ **50 µs**; reset input `VIL_nRST` in the (NA–0.6) V range. Keep the `CHIP_PU` trace as short as possible.
- For slow/unstable supplies or frequent power cycling, add a dedicated power-monitor/reset IC with a threshold around **3.0 V**.

## Strapping pins
- Strapping pins are **GPIO0, GPIO2, GPIO5, MTDI, MTDO**; after reset they act as normal IO.
- Boot mode is set by GPIO0/GPIO2: `GPIO0=1` → SPI/normal boot (default, GPIO2 = 0); `GPIO0=0` → download boot.
- **Recommend a pull-up on GPIO0.** Do **not** place a high-value capacitor on GPIO0 (the chip may wrongly enter download mode). Strapping hold time `tH` ≥ **3 ms** after CHIP_PU goes high.

## Sources
ESP32 Hardware Design Guidelines (Espressif Systems, Release master) — §1.3.3 Chip Power-up and Reset Timing, §1.3.5 Clock Source, §1.3.9 Strapping Pins.
URL: https://docs.espressif.com/projects/esp-hardware-design-guidelines/en/latest/esp32/ · accessed 2026-07-24.
