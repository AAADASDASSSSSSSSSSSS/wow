---
role: selection,schematic
title: Logic level-shifter selection
---

# Voltage level shifters (TXB0108 / TXS0102)

Rule for both: **VCCA ≤ VCCB** — the A side is the lower-voltage side, the B side the higher.

- **TXB0108** (8-bit, auto-direction): for **push-pull** signals (SPI, UART, GPIO). Its auto-direction drivers **fight external pull-ups**, so it is **not suitable for open-drain buses** (I²C). OE is referenced to VCCA.
- **TXS0102** (2-bit): designed for **open-drain (I²C) and push-pull**. Open-drain lines **require external pull-up resistors** to each side's rail (the part supplies only weak internal pull-ups).
- Do not exceed VCCB on any I/O; decouple both VCCA and VCCB.

## Sources
TXB0108 datasheet (TI); TXS0102 datasheet (TI). accessed 2026-07-24.
