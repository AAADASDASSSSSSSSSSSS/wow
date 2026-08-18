---
role: schematic
title: STM32F103 clock and boot configuration
---

# STM32F103 — clock & boot

Guidance specific to STM32F10xxx, from ST AN2586.

## HSE (main) crystal
- HSE external crystal/ceramic resonator range: **4–16 MHz** on STM32F101/102/103.
- Place the resonator and its **load capacitors as close as possible** to OSC_IN/OSC_OUT (minimizes distortion and start-up time).
- Load-cap sizing: `CL = CL1·CL2/(CL1+CL2) + Cstray`, with **CL1 = CL2**, high-quality ceramic in the **5–25 pF** range. Include PCB + pin capacitance (`Cstray`, ~**10 pF** rough estimate) — the crystal maker specifies the series-combination CL.
- An optional series resistor REXT (~5–6× the resonator series resistance) can be used; see ST AN2867 for tuning.

## LSE (RTC) crystal
- **32.768 kHz** LSE crystal for the RTC; place close to the pins. Use a resonator with **CL ≤ 7 pF** (never 12.5 pF) so CL1/CL2 stay under 15 pF.

## Boot configuration
- BOOT1/BOOT0 select the boot space (latched on the 4th SYSCLK rising edge after reset):
  - BOOT0 = 0 → **main flash** (normal run).
  - BOOT1 = 0, BOOT0 = 1 → **system memory** (built-in UART bootloader).
  - BOOT1 = 1, BOOT0 = 1 → **embedded SRAM**.
- Typical implementation uses **10 kΩ** resistors to set the BOOT pins.

## Source
ST AN2586 "Getting started with STM32F10xxx hardware development" — §3 Clocks, §4 Boot configuration.
URL: https://www.st.com/resource/en/application_note/an2586-getting-started-with-stm32f10xxx-hardware-development-stmicroelectronics.pdf · accessed 2026-07-24.
