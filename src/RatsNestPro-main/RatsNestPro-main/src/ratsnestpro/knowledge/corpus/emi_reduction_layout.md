---
role: emc,layout,routing
title: EMI reduction PCB layout
---

# EMI-reduction layout principles

From TI SZZA009 "PCB Design Guidelines for Reduced EMI".

- **Every fast signal edge is a current pulse**, and its **return current flows in the ground** underneath. Control the return path: route fast signals over a **continuous ground reference** so the return can flow directly beneath the trace.
- **Minimize loop area** of high-speed and switching signals (the loop = signal path + its return). Small loops radiate far less.
- Use a **solid ground plane**. Where only copper fills are possible (e.g. 2-layer), **grid the ground fills densely** with vias so they behave like a plane.
- Provide **many signal-return grounds** (return vias / ground pins) so return currents have short paths and do not detour.
- **Isolate the crystal/oscillator** and its load caps from other traces and keep its loop area small (it is both a noise source and sensitive).
- Combine with local **decoupling** (see decoupling corpus): local charge reduces supply-noise radiation.

## Source
TI SZZA009 "PCB Design Guidelines for Reduced EMI"; TI SZZA017A "High-Speed Layout Guidelines". accessed 2026-07-24.
