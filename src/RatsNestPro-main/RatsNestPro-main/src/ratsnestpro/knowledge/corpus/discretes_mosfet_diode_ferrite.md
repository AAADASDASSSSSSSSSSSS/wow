---
role: selection,schematic
title: Discrete MOSFETs, diodes and ferrite beads
---

# Discretes — MOSFETs, diodes, ferrite beads

## MOSFETs (AO3400 / AO3401, SOT-23)
- **AO3400**: 30 V **N-channel** (ID ~5.8 A at VGS = 10 V, RDS(on) < 28 mΩ) — low-side switch / load switch. It is a logic-level part; check RDS(on) at your actual VGS.
- **AO3401**: 30 V **P-channel** — the standard **reverse-polarity protection** device: put the P-MOS in series in the positive rail (source to input, drain to load) with the gate pulled to GND through a resistor; it conducts only for correct polarity, with far less drop than a series diode.

## Diodes
- **SS34 / SS3x** (Schottky, ~40 V, **3 A**, low forward drop): buck **catch/freewheel** diode, reverse-battery blocking, or OR-ing — use Schottky wherever low Vf / fast recovery matters.
- **1N4148W**: SMD fast small-signal switching diode for logic/clamp/steering (not for power).

## Ferrite beads (Murata EMIFIL etc.)
- Use a ferrite bead to filter **high-frequency** noise on a supply rail or to **isolate an analog supply** (e.g. VDDA/AVCC) from the digital rail. Select by **impedance at 100 MHz**, adequate **DC current rating**, and **low DC resistance**; a bead + capacitor forms a low-pass LC.

## Sources
AO3400 datasheet (AOS); AO3401 datasheet (AOS); Vishay SS32/SS33/SS34 datasheet; 1N4148W datasheet (Vishay/Nexperia); Murata EMIFIL selection (Murata). accessed 2026-07-24.
