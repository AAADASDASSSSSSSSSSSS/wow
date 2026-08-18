---
role: dfm,layout,routing
title: PCB fab process limits and assembly (DFM)
---

# Fabrication limits & assembly (DFM)

Design-for-manufacture targets from JLCPCB / PCBWay capability pages. **Design to conservative values with margin — not the vendor's absolute edge-of-process minimums** (edge values lower yield).

## Process minimums (JLCPCB standard, 1 oz, 2-layer)
- **Min track width / spacing**: vendor minimum ~**0.10 mm**; a safe internal default is **≥ 0.127 mm** (0.15 mm+ preferred for cost/yield).
- **Vias**: vendor min **0.15 mm drill / 0.25 mm diameter**; conservative default **0.3 mm / 0.45 mm**.
- **PTH annular ring**: **≥ 0.15 mm** absolute, **0.20 mm recommended**.
- **Min hole (drill)**: ~**0.15 mm** vendor min.
- **Silkscreen line width**: **≥ 0.15 mm** (thinner may not render).
- **Board-edge copper clearance**: keep copper **≥ ~0.3 mm** from the board edge.

## Controlled impedance
- JLCPCB and PCBWay offer controlled-impedance fabrication with **~±10 % tolerance** (≤ 50 Ω lines: about ±5 Ω). It requires a **defined stackup** — coordinate track width/gap with the fab's impedance calculator.

## Assembly (SMT)
- Respect the assembler's placeable **package range** and min/max component sizes; keep enough **part-to-part spacing / courtyard clearance**.
- Provide **fiducials** for machine assembly; keep component orientations consistent where possible; avoid components too close to the board edge or to tall neighbours.

## Sources
JLCPCB PCB Capabilities / Impedance / PCB Assembly Capabilities pages; PCBWay Capabilities / Impedance Control pages. accessed 2026-07-24.
