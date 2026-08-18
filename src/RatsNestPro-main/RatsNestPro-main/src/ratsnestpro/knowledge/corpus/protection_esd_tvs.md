---
role: selection,schematic,layout
title: ESD and TVS protection
---

# ESD / TVS protection

Protect exposed connector pins and power inlets from ESD and surges.

## ESD on data connectors
- **USBLC6-2**: low-capacitance ESD array for **USB** (D+ / D− and VBUS), **IEC 61000-4-2 level 4** compliant, USB 2.0 compliant (low capacitance so it does not spoil the differential pair). Place it **right at the USB connector**, before the traces run into the board.
- **PESD5V0L1BA**: single-line 5 V ESD protection diode for a general I/O line.

## TVS on power inlets
- **SMAJ** series: TVS diodes for surge/transient clamping. Pick a **standoff (working) voltage ≥ the rail's normal voltage** (so it does not conduct normally); use **unidirectional** for DC rails. Place at the power entrance.

## Layout
- Put ESD/TVS devices **as close as possible to the connector/entry**, with a short, low-inductance path to ground; the protected trace should pass the device first.

## Sources
USBLC6-2 datasheet (ST); PESD5V0L1BA datasheet (Nexperia); SMAJ series datasheet (Littelfuse, via Internet Archive snapshot). accessed 2026-07-24.
