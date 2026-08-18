---
role: selection,schematic,layout
title: ESP32-C3 power, clock, reset and RF
---

# ESP32-C3 — power, clock, reset & RF

Guidance specific to the Espressif ESP32-C3 (RISC-V, Wi-Fi/BLE). Similar to ESP32 with some differences noted.

## Power
- Single supply: recommended **3.3 V, ≥ 500 mA**. Main power entrance: **ESD protection diode + ≥ 10 µF**.
- Digital **VDD3P3_CPU** (pin 17), range **3.0–3.6 V**: add **0.1 µF** close to the pin.
- **VDD_SPI** powers 3.3 V flash via an internal resistor from VDD3P3_CPU (expect a small voltage drop); add **1 µF** close, and keep it **≥ 3.0 V** for the flash. When not powering flash it can be a GPIO.
- Analog **VDDA/VDD3P3** (3.0–3.6 V): add **10 µF** to VDD3P3 (works with the 0.1 µF) to handle RF-TX current surges; add an **LC filter** on VDD3P3 (inductor rated ≥ 500 mA). **VDD3P3_RTC**: 0.1 µF (cannot be a standalone backup supply).

## Power-up / reset (CHIP_EN)
- `CHIP_EN` high = enable, low = reset; **must not float**. Add an **RC delay: R = 10 kΩ, C = 1 µF**. Timing minimums `tSTBL`/`tRST` ≥ **50 µs**. Keep the CHIP_EN trace short; for unstable supplies add a power-monitor/reset IC (~3.0 V threshold).

## Clock
- ESP32-C3 firmware supports **only a 40 MHz** crystal, accuracy **±10 ppm**. Put a **series inductor (~24 nH initially)** on the XTAL_P trace (tune after RF test). Load caps from `CL = C1·C2/(C1+C2+Cstray)`; crystal amplitude **> 500 mV**.
- Optional **32.768 kHz** RTC crystal: ESR ≤ 70 kΩ, bias parallel R 5–10 MΩ (usually not populated).

## Flash & RF
- External flash up to 16 MB on VDD_SPI; add **0 Ω series footprints** on the SPI lines for tuning/EMI flexibility.
- RF traces need **50 Ω** controlled impedance; keep the CLC matching **close to the chip** (0201 parts); antenna characteristic impedance ~**50 Ω**.

## Source
ESP32-C3 Hardware Design Guidelines (Espressif, Release master) — §1.3.2 Power, §1.3.3 Power-up/Reset, §1.3.4 Flash, §1.3.5 Clock, §1.3.6 RF.
URL: https://docs.espressif.com/projects/esp-hardware-design-guidelines/en/latest/esp32c3/ · accessed 2026-07-24.
