---
role: layout,routing
title: RP2040 USB routing and board layout
---

# RP2040 — USB routing & board layout

Guidance specific to the Raspberry Pi RP2040 (full-speed USB, 56-pin QFN).

## USB (full-speed) routing
- **USB_DP / USB_DM need no external pull-ups/pull-downs** (built into the I/Os).
- Add **27 Ω series termination resistors** on DP and DM, placed **close to the chip**, to meet the USB impedance spec.
- Route DP/DM as a **~90 Ω differential** pair. Example on a **1 mm-thick 2-layer** board: **0.8 mm track width, 0.15 mm gap** ≈ 90 Ω differential. Keep a **solid, uninterrupted ground** directly under the USB pair for its whole length. Thicker boards require re-engineering the geometry.

## I/O headers & ground
- Provide **many ground pins** on I/O connectors (e.g. an outer ground row on 2.54 mm headers): keeps a low-impedance ground and gives return-current paths, reducing EMI from fast switching signals.

## Stackup choices
- **Minimal / low-cost**: 2-layer, components on the top side only, relaxed design rules (verified against a standard low-cost PCB pool).
- **More demanding**: 4-layer, 1.6 mm, with **dedicated power and ground planes** — improves supply decoupling and signal integrity.

## Keep short
- QSPI flash bus and the crystal loop should use **short** traces to preserve signal integrity and keep crystal load capacitance predictable.

## Source
Hardware design with RP2040 (Raspberry Pi) — §2.4 I/Os (USB, headers), §2.7 Making a PCB, Ch.3 (4-layer plane design).
URL: https://datasheets.raspberrypi.com/rp2040/hardware-design-with-rp2040.pdf · accessed 2026-07-24.
