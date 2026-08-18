---
role: selection,schematic
title: Linear regulator (LDO) selection and decoupling
---

# Linear regulators (LDOs) — selection & externals

LDOs drop a higher input to a lower fixed output. A hard rule: the input must
be **≥ Vout + dropout**, so an LDO **cannot** regulate an output equal to its
input (e.g. 5 V → 5 V, or 3.3 V → 3.3 V, is impossible — use the source rail or
a boost converter instead).

## Common parts and their required externals
- **AMS1117** (1 A): dropout ~**1.1 V** (works down to ~1 V). Needs a **22 µF tantalum output** capacitor as part of its frequency compensation (stability). Good for 5 V → 3.3 V; **not** usable for 5 V → 5 V.
- **AP2112K-3.3** (600 mA): needs **1 µF** input and output (ceramic X5R/X7R). Max output 3.3 V (no 5 V variant exists).
- **MIC5219** (500 mA): needs a **2.2 µF** output capacitor.
- **LP2985** (150 mA, low-noise): add a **10 nF** noise-reduction/bypass capacitor for its ~30 µVRMS low-noise spec.

## General rules
- Place the input and output caps close to the LDO pins.
- Use an LDO only for **small drops / low-noise** rails; for large step-downs prefer a switching regulator (efficiency).

## Sources
AMS1117 datasheet (Advanced Monolithic Systems); AP2112 datasheet (Diodes DS39724); MIC5219 datasheet (Microchip); LP2985 datasheet (TI). accessed 2026-07-24.
