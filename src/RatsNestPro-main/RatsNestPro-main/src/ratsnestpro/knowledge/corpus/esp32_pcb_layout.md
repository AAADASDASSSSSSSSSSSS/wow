---
role: layout,routing
title: ESP32 PCB layout and RF routing
---

# ESP32 — PCB layout & RF routing

Guidance specific to the Espressif ESP32 SoC (scope: ESP32 series).

## Stackup
- Recommended **four-layer** stackup:
  - L1 (TOP): signal traces + components.
  - L2 (GND): complete GND plane, **no signal traces**.
  - L3 (POWER): power traces + a few signals, but keep a complete GND plane under the RF and crystal.
  - L4 (BOTTOM): a few signal traces; **no components**.
- **Two-layer** alternative: TOP = signals + components; BOTTOM = no components, minimal traces, and a **continuous GND plane** under the chip, RF, and crystal.

## Power routing
- Prefer routing power on inner layers (not the GND layer); use **≥ 2 vias** where a main power trace crosses layers.
- Power trace widths: **main power ≥ 25 mil**, **VDD3P3 ≥ 20 mil**, other power **12–15 mil**; surround power traces with ground copper.
- Place the ESD protection diode close to the power input. Add a **10 µF** cap before power enters the chip (optionally 0.1 µF or 1 µF in parallel), then branch power in a **star layout** to reduce pin-to-pin coupling.
- RF power pins 3 and 4: **10 µF each**, plus a **CLC/LC filter** near the pins; use **0201** components (except the 10 µF) and route at 45° away from RF traces.
- Ground pad under the chip: connect to the GND plane with **at least nine ground vias**; ground pads should contact the copper pour directly (not through traces). Add ground vias next to each decoupling cap's ground pad for a short return path.

## RF & crystal
- RF traces require **50 Ω** controlled impedance. Keep the chip matching circuit (CLC preferred) **close to the chip**; the antenna characteristic impedance should be ~**50 Ω**; add ESD protection for the antenna. Use **0201** packages for RF matching parts.
- Place the crystal close to the chip with surrounding ground; keep the oscillator loop short.

## Sources
ESP32 Hardware Design Guidelines (Espressif Systems, Release master) — §1.3.6 RF, §1.4.1 General Principles of PCB Layout, §1.4.2 Power Supply layout, §1.4.3 Crystal layout.
URL: https://docs.espressif.com/projects/esp-hardware-design-guidelines/en/latest/esp32/ · accessed 2026-07-24.
