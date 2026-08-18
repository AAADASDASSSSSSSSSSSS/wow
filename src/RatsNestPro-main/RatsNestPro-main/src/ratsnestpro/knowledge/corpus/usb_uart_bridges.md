---
role: selection,schematic
title: USB-to-UART bridge design
---

# USB-to-UART bridges (CP2102 / CH340 / FT232RL)

These present a USB full-speed device and expose a UART.

- Wire **USB D+ / D−** to the connector and follow USB routing rules (short, ~90 Ω differential, ground under the pair; series/ESD as needed). See USB layout guidance.
- **No external crystal needed** — CP2102, CH340C and FT232RL all have an internal oscillator.
- Add local **decoupling** on the supply pins.
- **Bus-powered vs self-powered**: CP2102 and FT232RL integrate a 3.3 V regulator (FT232RL provides a VCC3V3 output, ~50 mA) that can power the chip's I/O; decide per the datasheet and connect VCCIO accordingly.
- A common design adds a UART TXD series resistor and, on MCU boards, wires DTR/RTS for auto-reset/boot.

## Sources
CP2102 datasheet (Silicon Labs); CH340 datasheet (WCH); FT232RL datasheet (FTDI, via Internet Archive snapshot). accessed 2026-07-24.
