---
role: selection,schematic
title: ESP32 power supply and decoupling
---

# ESP32 — power supply & decoupling

Guidance specific to the Espressif ESP32 SoC (scope: ESP32 series, not other MCUs).

## Supply rails
- Single-supply designs: recommended supply **3.3 V**, main supply current **≥ 500 mA**.
- Main power entrance (where external power enters the PCB): add an **ESD protection diode** and **at least one 10 µF** capacitor.
- Digital power pins — `VDD3P3_CPU` (pin 37, 1.8–3.6 V) and `VDD3P3_RTC` (pin 20, 2.3–3.6 V): place a **0.1 µF** capacitor close to each digital power pin.
- Analog power `VDD3P3` (pins 3, 4) and `VDDA` (pins 1, 43, 46), 2.3–3.6 V: `VDD3P3` can see current surges during RF TX, so add a **10 µF** capacitor to the rail (working with the 1 µF caps). Add an **LC filter** on `VDD3P3` to suppress high-frequency harmonics; the inductor rated current should preferably be **≥ 500 mA**.
- `VDD_SDIO` supplies external/internal flash/PSRAM at 3.3 V (default, via an internal ~6 Ω resistor from `VDD3P3_RTC`) or 1.8 V (internal LDO, max 40 mA). In 3.3 V mode add a **1 µF** filter cap close to the pin; in 1.8 V mode add a **2 kΩ pull-down + 4.7 µF** to ground. Keep `VDD3P3_RTC` above 3.0 V when it feeds 3.3 V flash/PSRAM.

## Required external capacitor
- Pin 48 `CAP1`: capacitor **C5 = 10 nF, ±10 % tolerance is required** for proper ESP32 operation.

## ADC
- Add a **0.1 µF** filter cap between the ADC input pin and ground to improve accuracy.
- Prefer **ADC1** over ADC2 (ADC2 cannot be used while Wi-Fi is enabled).

## Sources
ESP32 Hardware Design Guidelines (Espressif Systems, Release master) — §1.3.2 Power Supply, §1.3.11 ADC, §1.3.12 External Capacitor.
URL: https://docs.espressif.com/projects/esp-hardware-design-guidelines/en/latest/esp32/ · accessed 2026-07-24.
