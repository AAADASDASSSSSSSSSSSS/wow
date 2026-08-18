---
role: routing,layout
title: High-speed differential pair routing
---

# High-speed differential pairs

Applies to USB (90 Ω), Ethernet / LVDS (100 Ω) and similar pairs.

- Route the pair as a **tightly-coupled differential pair** with the target **differential impedance** (USB 2.0 = **90 Ω**; Ethernet/LVDS = **100 Ω**) set by track width/gap and the stackup.
- **Length-match within the pair** (minimize intra-pair skew) and keep the pair **short**.
- Route over a **continuous ground reference** for the whole length; do **not** cross plane splits or gaps (that breaks the return path).
- **Minimize vias and stubs** on the pair; keep both legs symmetric.
- **Series termination** (e.g. 27 Ω on USB DP/DM) close to the driver where the device requires it.
- USB pull-up: some MCUs embed the USB_DP pull-up, but others **require an external 1.5 kΩ pull-up on USB_DP** — check the specific device (ST AN4879 lists this per STM32 series).

## Sources
USB 2.0 specification (USB-IF); ST AN4879 "USB hardware and PCB guidelines using STM32 MCUs"; LAN8720A datasheet (100 Ω pairs); TI SZZA017A "High-Speed Layout Guidelines". accessed 2026-07-24.
