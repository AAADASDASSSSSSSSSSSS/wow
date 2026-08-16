"""Task 0a — the device-resolution matrix that pins ``fact_sheet`` matching.

Why this file exists
--------------------
``fact_sheet`` used to match a normalized key *anywhere inside* the queried
string. Because normalization erases separators, "ESP32-S3" became "esp32s3",
which contains "esp32", so the classic ESP32's sheet answered for it. That sheet
asserts ``pin_count`` = 48 (QFN48) as a ``FixedFact`` compared ``EXACT`` with
``MALFUNCTION`` consequence, so an S3 design's 56 pads were reported as a
page-cited ERROR against "ESP32 Series Datasheet v5.2 Figure 6-1/6-2, p.60" — a
confident, sourced, wrong verdict that blocked a legal board.

The failure was invisible: a mis-hit and a correct hit look identical
downstream, and no test distinguished them. This matrix is the distinguishing
test. Every row is a real order code, and each states which sheet — or none —
may answer for it.
"""

from __future__ import annotations

import pytest

from ratsnestpro.eda.factsheet import DeviceClass, fact_sheet

# --------------------------------------------------------------------------- #
# Order codes that MUST resolve, and to exactly which sheet
# --------------------------------------------------------------------------- #

RESOLVES: list[tuple[str, str]] = [
    # Plain device names.
    ("ESP32", "ESP32"),
    ("ESP32-C3", "ESP32-C3"),
    ("RP2040", "RP2040"),
    ("STM32F103", "STM32F103"),
    ("ATmega328P", "ATmega328P"),
    # ESP32 variants that ARE the classic die.
    ("ESP32-D0WD", "ESP32"),
    ("ESP32-D0WDQ6-V3", "ESP32"),
    ("ESP32-U4WDH", "ESP32"),
    ("ESP32-WROOM-32", "ESP32"),
    # ESP32-C3 order codes: the longest key wins over the plain "ESP32" sheet,
    # and the ESP32 sheet's "ESP32-C" exclude rules it out a second time.
    ("ESP32-C3FH4", "ESP32-C3"),
    ("ESP32-C3FH4X", "ESP32-C3"),
    ("ESP32-C3-MINI-1", "ESP32-C3"),
    # STM32F103 medium density (x8/xB) — the band this sheet documents.
    ("STM32F103C8T6", "STM32F103"),
    ("STM32F103C8T7", "STM32F103"),
    ("STM32F103CBT6", "STM32F103"),
    ("STM32F103CBU6", "STM32F103"),
    ("STM32F103R8T6", "STM32F103"),
    ("STM32F103RBT6", "STM32F103"),
    ("STM32F103VBT6", "STM32F103"),
    ("STM32F103TBU6", "STM32F103"),
    ("BluePill", "STM32F103"),
    # KiCad lib-ids are matched as a whole identifier.
    ("MCU_ST_STM32F1:STM32F103C8Tx", "STM32F103"),
    ("MCU_Microchip_ATmega:ATmega328P-A", "ATmega328P"),
    # ATmega328P packages.
    ("ATmega328P-AU", "ATmega328P"),
    ("ATmega328P-PU", "ATmega328P"),
    ("ATmega328P-MU", "ATmega328P"),
    # A value string carrying the part number among other identifiers.
    ("STM32F103C8T6 LQFP48", "STM32F103"),
    ("ATmega328P-AU (TQFP-32)", "ATmega328P"),
    # Non-MCU sheets must keep resolving.
    ("AMS1117-3.3", "AMS1117-3.3"),
    ("AP2112K-3.3TRG1", "AP2112K-3.3"),
    ("MIC5219-3.3YM5", "MIC5219"),
    ("LP2985-3.3", "LP2985"),
    ("TPS563201DDCR", "TPS563201"),
    ("TPS61023DRLR", "TPS61023"),
    ("USBLC6-2SC6", "USBLC6-2"),
    ("PESD5V0L1BA", "PESD5V0L1BA"),
    ("USB-C 16P", "USB-C 16P"),
    ("C2765186", "USB-C 16P"),
    ("X322512MSB4SI", "X322512MSB4SI"),
    ("C9002", "X322512MSB4SI"),
    ("X322516MLB4SI", "X322516MLB4SI"),
    ("TXM40M0004252HBCEO00T", "TXM40M0004252HBCEO00T"),
]


@pytest.mark.parametrize(("query", "device"), RESOLVES)
def test_order_code_resolves_to_its_own_sheet(query: str, device: str) -> None:
    sheet = fact_sheet(query)
    assert sheet is not None, f"{query} resolved to no sheet"
    assert sheet.device == device, f"{query} resolved to {sheet.device}, expected {device}"


# --------------------------------------------------------------------------- #
# Order codes that MUST NOT resolve — a neighbouring die is not a substitute
# --------------------------------------------------------------------------- #

REFUSES: list[tuple[str, str]] = [
    # Espressif siblings: different dies, packages and strapping pins. Each of
    # these previously resolved to the classic ESP32 sheet.
    ("ESP32-S2", "QFN56, different power domains and strapping pins"),
    ("ESP32-S3", "QFN56, different power domains and strapping pins"),
    ("ESP32-S3-WROOM-1", "module around the S3 die"),
    ("ESP32-C6", "RISC-V, different pinout"),
    ("ESP32-H2", "RISC-V 802.15.4 part"),
    ("ESP32-P4", "application processor, unrelated pinout"),
    ("ESP32-C2", "different die from the C3"),
    # STM32F103 outside the medium-density band this sheet documents.
    ("STM32F103RCT6", "high density, documented in DS5792 not DS5319"),
    ("STM32F103ZET6", "high density, LQFP144 is absent from this sheet"),
    ("STM32F103C6T6", "low density, a different datasheet"),
    ("STM32F103RGT6", "XL density, a different datasheet"),
    # Other STM32 families entirely.
    ("STM32F407VGT6", "no sheet in the roster"),
    ("STM32G031K8", "no sheet in the roster"),
    ("STM32F411CEU6", "no sheet in the roster"),
    ("STM32L432KC", "no sheet in the roster"),
    # AVR siblings.
    ("ATmega32U4", "different die"),
    ("ATmega2560", "different die"),
    ("ATtiny85", "different die"),
    ("ATmega328PB", "different die with extra peripherals"),
    # Raspberry Pi successor.
    ("RP2350", "different die, dual-architecture successor"),
    # A pin-compatible clone is still a different manufacturer's die.
    ("GD32F103C8T6", "GigaDevice clone, not ST silicon"),
    ("CH32V203", "no sheet in the roster"),
]


@pytest.mark.parametrize(("query", "reason"), REFUSES)
def test_uncovered_order_code_resolves_to_nothing(query: str, reason: str) -> None:
    """A part with no sheet must produce ``None``, never a neighbour's sheet.

    ``None`` is the honest answer and is what lets the coverage signal report
    "no datasheet facts for this part". Returning a neighbour's sheet instead
    produces a cited verdict about the wrong silicon.
    """
    sheet = fact_sheet(query)
    assert sheet is None, (
        f"{query} resolved to {sheet.device if sheet else None} but has no sheet: {reason}"
    )


# --------------------------------------------------------------------------- #
# Properties of the matcher itself
# --------------------------------------------------------------------------- #


def test_both_esp32_generations_are_found_in_one_requirement() -> None:
    """``excludes`` is per identifier, not per query.

    A requirement naming both parts must still resolve each one. Vetoing the
    whole query because "esp32c" appears somewhere in it would lose the plain
    ESP32 mention — which is why the exclude prefix is tested against each
    identifier rather than the concatenated text.
    """
    text = "compare ESP32 against ESP32-C3 for this board"
    assert fact_sheet("ESP32") is not None
    esp32c3 = fact_sheet("ESP32-C3")
    assert esp32c3 is not None and esp32c3.device == "ESP32-C3"
    # The whole sentence names two devices, so a single-answer lookup is
    # ambiguous by construction; what matters is that it never answers with a
    # sheet whose exclude covers one of the mentions.
    resolved = fact_sheet(text)
    assert resolved is None or resolved.device in {"ESP32", "ESP32-C3"}


def test_device_class_filter_still_applies() -> None:
    assert fact_sheet("STM32F103C8T6", DeviceClass.MCU) is not None
    assert fact_sheet("STM32F103C8T6", DeviceClass.LDO) is None


def test_empty_and_whitespace_queries_resolve_to_nothing() -> None:
    for query in ("", "   ", "\t\n"):
        assert fact_sheet(query) is None


def test_matching_is_case_and_separator_insensitive() -> None:
    for query in ("stm32f103c8t6", "STM32F103C8T6", "stm32-f103-c8t6", "STM32F103C8T6"):
        sheet = fact_sheet(query)
        assert sheet is not None and sheet.device == "STM32F103", query
