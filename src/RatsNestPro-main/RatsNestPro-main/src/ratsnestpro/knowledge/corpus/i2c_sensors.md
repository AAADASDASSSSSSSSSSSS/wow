---
role: selection,schematic
title: I2C sensor design (temp/humidity, pressure, IMU)
---

# I²C sensors — common design rules

Covers SHT31 / AHT20 (temp-humidity), BMP280 (pressure), MPU6050 / LSM6DS3 (IMU).

## General
- Place a **100 nF decoupling capacitor as close as possible to VDD** (Sensirion states this explicitly for SHT31).
- **SDA and SCL need pull-up resistors to VDD** — typically **~10 kΩ** (use lower, e.g. 4.7 kΩ or 2.2 kΩ, for higher speed or more bus capacitance). Only **one** pull-up pair per bus, not per device.
- Use each device's **address-select pin** to avoid address collisions when sharing the bus (e.g. SHT31 = 0x44 default / 0x45 via the ADDR pin).

## Per-part notes
- **SHT31**: two selectable I²C addresses, up to 1 MHz; 100 nF close to VDD.
- **AHT20**: I²C temp/humidity (Aosong).
- **BMP280**: I²C or SPI pressure sensor; decouple **VDD and VDDIO** separately.
- **MPU6050 / LSM6DS3** (IMU): I²C or SPI; have **separate VDD and VDDIO** rails — decouple **both**; the address/interface-select pin (AD0 / SDO) picks the I²C address or SPI mode.

## Sources
SHT31 datasheet (Sensirion, via Internet Archive snapshot); AHT20 datasheet (Aosong); BMP280 datasheet (Bosch Sensortec); MPU6050 datasheet (InvenSense/TDK, via Internet Archive snapshot); LSM6DS3 datasheet (ST, via Internet Archive snapshot). accessed 2026-07-24.
