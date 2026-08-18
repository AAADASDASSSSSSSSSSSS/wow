---
role: selection,schematic,layout
title: Ethernet PHY (RMII) design
---

# Ethernet PHY — LAN8720A (RMII 10/100)

- **LAN8720A** is a small-footprint **RMII** 10/100 Ethernet PHY with HP Auto-MDIX. It can use a low-cost **25 MHz crystal** to reduce BOM; the RMII reference clock is **50 MHz** (either supplied to the PHY or output by it — keep the clocking scheme consistent across MAC and PHY).
- Between the PHY and the RJ45 you need **Ethernet magnetics** (isolation transformer), often integrated in a MagJack.
- Route **TD± and RD± as 100 Ω differential pairs**, length-matched within each pair, over a solid ground; keep them short and away from noise.
- Provide clean, well-decoupled supplies; follow the PHY's strap-pin requirements for PHY address / mode at reset.

## Source
LAN8720A datasheet (Microchip). accessed 2026-07-24.
