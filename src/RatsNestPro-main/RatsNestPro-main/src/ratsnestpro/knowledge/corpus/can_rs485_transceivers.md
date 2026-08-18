---
role: selection,schematic
title: CAN and RS-485 transceiver design
---

# CAN and RS-485 transceivers

Differential multi-drop buses — termination is mandatory.

## CAN (CANH / CANL)
- Terminate **each end of the bus with 120 Ω**. A common refinement is **split termination**: two 60 Ω resistors in series with a common cap (~4.7 nF) to GND at the midpoint (improves EMC).
- **SN65HVD230**: 3.3 V CAN transceiver with an **RS slope-control pin** (a resistor to GND sets the slew rate; also gives a low-power standby). High input impedance allows up to 120 nodes.
- **TJA1051**: 5 V CAN transceiver.
- Place the transceiver near the connector; keep the CANH/CANL pair together.

## RS-485 (A / B)
- **MAX485**: half-duplex differential transceiver. Terminate **both ends of the bus with 120 Ω**. Drive **DE/RE** for direction control. Add **fail-safe bias resistors** (pull A high / B low) so the idle bus is defined.

## Sources
SN65HVD230 datasheet (TI); TJA1051 datasheet (NXP); MAX485/MAX1487-MAX491 datasheet (Analog Devices/Maxim). accessed 2026-07-24.
