---
role: selection,schematic,layout
title: Switching regulator (buck/boost) selection and layout
---

# Switching regulators — buck / boost

Switching regulators are far more efficient than LDOs for large step-downs or
for stepping **up**. Use them when an LDO's dropout/heat is unacceptable.

## Topologies & common parts
- **Buck (step-down)**: LM2596 (asynchronous, needs an external **catch Schottky diode**), TPS563201 (synchronous), TPS62840 (high-efficiency, low-Iq), MP1584.
- **Boost (step-up)**: MT3608, TPS61023.

## Required externals
- **External inductor**: its **saturation current rating must exceed the peak inductor current** (peak = load + ½ ripple). Undersizing causes saturation and failure.
- **Input capacitor**: a ceramic cap placed **very close** to the IC's input pin (supplies the switching current pulses).
- **Output LC**: inductor + output capacitor form the filter; size for the target ripple and loop stability.
- Asynchronous bucks (e.g. LM2596) additionally need a **catch/freewheel Schottky diode**.

## Layout (critical for switchers)
- Keep the **switching "hot loop"** (input cap → high-side switch → diode/low-side → back to cap) **as small and tight as possible** — this loop carries fast di/dt and is the main EMI source.
- Place the input cap adjacent to the IC; keep the SW node small (it is a noise aggressor — do not route sensitive traces near it); use a solid ground with plenty of vias.

## Sources
LM2596 datasheet (TI); TPS563201 datasheet (TI) §7.4 Layout; TPS62840 datasheet (TI); MT3608 datasheet (Aerosemi); TPS61023 datasheet (TI). accessed 2026-07-24.
