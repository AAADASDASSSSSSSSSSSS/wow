---
role: selection,schematic
title: Op-amp, ADC and voltage-reference design
---

# Op-amps, ADCs and references

## Op-amps (LM358, MCP6002)
- Place a **0.1 µF** decoupling cap close to the supply pin(s).
- **LM358** is single-supply capable but is **not** rail-to-rail (limited output swing near the rails; input common-mode does not include the top rail). **MCP6002** is a rail-to-rail CMOS op-amp.
- Respect the input common-mode range and output-swing limits when choosing one for a given rail.

## ADC (ADS1115)
- 16-bit **I²C** ADC, supply **2.0–5.5 V**, with an **internal reference and oscillator** and a programmable comparator.
- **Four pin-selectable I²C addresses** (ADDR pin) → up to four on one bus.
- Add a 0.1 µF supply decoupling cap; filter/limit the analog inputs to the supply range.

## Voltage reference (TL431)
- Precision **programmable shunt reference** (adjustable shunt regulator). Set the output with the REF resistor divider.
- Feed it through a **series cathode resistor** from the supply; keep the **cathode current between 1 mA and 100 mA** (below 1 mA it is out of regulation).

## Sources
LM358 datasheet (TI); MCP6002 datasheet (Microchip, via Internet Archive snapshot); ADS1115 datasheet (TI); TL431 datasheet (TI). accessed 2026-07-24.
