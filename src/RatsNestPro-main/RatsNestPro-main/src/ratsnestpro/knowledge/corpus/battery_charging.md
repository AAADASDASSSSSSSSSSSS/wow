---
role: selection,schematic
title: Li-ion battery charging (linear chargers)
---

# Single-cell Li-ion charging (linear chargers)

Common single-cell Li-ion/LiPo linear chargers, typically fed from 5 V USB.

## TP4056 (1 A standalone linear charger)
- Fixed **4.2 V** float voltage; **charge current set by one resistor on PROG** (the BAT-to-PROG current ratio is 1200 : 1, i.e. `Ichg ≈ 1200 × VPROG / RPROG`).
- Charge cycle **terminates at 1/10** of the programmed current after the float voltage is reached.
- **Thermal feedback** regulates the charge current to limit die temperature — provide adequate copper/thermal relief.

## MCP73831 (linear charger)
- Regulation-voltage variants: **4.20 / 4.35 / 4.40 / 4.50 V** (pick to match the cell).
- Regulation current **IREG set by the PROG resistor** (e.g. **RPROG = 10 kΩ → IREG = 100 mA**; valid 2.0–10 kΩ). Precondition current ≈ 10 % IREG, termination ≈ 5 % IREG.
- Provide a thermal pad / copper for heat.

## General
- Add an input capacitor near VDD/IN (USB 5 V input). Keep charge current within the cell's and the connector's ratings, and size copper for the charge current + charger dissipation.

## Sources
TP4056 datasheet (NanJing Top Power); MCP73831 datasheet (Microchip DS20001984). accessed 2026-07-24.
