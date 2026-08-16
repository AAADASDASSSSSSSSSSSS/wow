"""What the requirement does not say, decided by code rather than by a model.

``clarify_missing_data`` could only ask about values the Architect volunteered in
its ``ratsnest-assumptions`` block. That block is model output, so every way the
model can fail — a gateway timeout, a reply that ignores the format, malformed
JSON — silently produced an empty list, and a run with nothing decided asked
nothing. The gap is real either way; only the record of it was missing.

This module derives the same records from the requirement text alone. A slot is
asked about when the text does not state it and the board plausibly needs it, so
the question survives a dead LLM, a dead search provider and an empty parts
cache. The Architect's own assumptions still win on a slot both produce: those
came from the fact sheets and carry a citation, while everything here carries
only a convention or a declared engineering default and says so.

Two rules keep this from becoming noise. A slot the requirement already settles
is never raised — a fully specified requirement must still run start to finish
without a single question. And a value this module cannot justify is delegated
rather than invented: the clock option says "the frequency the datasheet
requires" and leaves the number to the phase holding the fact sheets, the same
move :mod:`agents.ratsnestpro.decisions` makes for a violated limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agents.language import localized
from agents.ratsnestpro.capability import strip_runtime_evidence

# Model-facing basis label for a value no source states. Kept honest on purpose:
# the report distinguishes a cited fact from a default someone chose.
_ENGINEERING_DEFAULT = {"en": "engineering default", "zh": "工程默认值"}
_CONVENTION = {"en": "board-level convention", "zh": "板级惯例"}


@dataclass(frozen=True)
class Gap:
    """One slot that may be missing, and what to propose when it is.

    ``stated_when`` and ``stated_pattern`` are the negative test: either one
    matching means the user already decided this, so it is not a gap. They are
    deliberately generous — a false "already stated" costs one unasked question,
    while a false gap interrogates someone who answered already.

    ``assumed`` and ``alternatives`` are model-facing and stay in English
    regardless of the reply language, because they are appended verbatim to the
    requirement every later phase reads.
    """

    slot: str
    question: dict[str, str]
    assumed: str
    basis: dict[str, str]
    alternatives: tuple[str, ...] = ()
    stated_when: tuple[str, ...] = ()
    stated_pattern: str = ""
    applies_when: tuple[str, ...] = ()

    def relevant(self, lowered: str) -> bool:
        if not self.applies_when:
            return True
        return any(token in lowered for token in self.applies_when)

    def stated(self, lowered: str) -> bool:
        if any(token in lowered for token in self.stated_when):
            return True
        return bool(self.stated_pattern) and bool(re.search(self.stated_pattern, lowered))

    def record(self, language: str) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "question": localized(self.question, language),
            "assumed": self.assumed,
            "basis": localized(self.basis, language),
            "alternatives": list(self.alternatives),
        }


# Ordered by consequence: ``assumption_decisions`` caps the form at six items, so
# the ones that change the layout or the bill of materials come first.
GAPS: tuple[Gap, ...] = (
    Gap(
        slot="board_outline",
        question={
            "en": "Board outline — no dimensions given. What size should it be?",
            "zh": "板子外形 —— 需求里没写尺寸。做多大？",
        },
        assumed="50 x 40 mm rectangular outline with 1 mm corner radii",
        basis=_ENGINEERING_DEFAULT,
        alternatives=(
            "80 x 50 mm rectangular outline",
            "Outline fixed by an enclosure; wait for a supplied DXF or drawing",
        ),
        # A bare "60x40" counts, and so does any explicit mention of size.
        stated_pattern=r"\d+\s*(?:x|×|\*)\s*\d+",
        stated_when=("board size", "outline", "尺寸", "外形", "板框", "长宽"),
    ),
    Gap(
        slot="layer_count",
        question={
            "en": "Layer count — not stated. How many copper layers?",
            "zh": "层数 —— 需求里没写。做几层板？",
        },
        assumed="2 copper layers",
        basis=_CONVENTION,
        alternatives=("4 copper layers", "6 copper layers"),
        stated_when=(
            "two layer",
            "two-layer",
            "2 layer",
            "2-layer",
            "four layer",
            "four-layer",
            "4 layer",
            "4-layer",
            "6 layer",
            "两层",
            "双层",
            "2层",
            "四层",
            "4层",
            "六层",
            "多层",
            "单层",
        ),
    ),
    Gap(
        slot="input_power",
        question={
            "en": "Input power — not stated. Where does the board get its power?",
            "zh": "输入电源 —— 需求里没写。板子从哪取电？",
        },
        assumed="USB-C receptacle used as a 5 V sink only, with the sink-side CC pulldowns",
        basis=_ENGINEERING_DEFAULT,
        alternatives=(
            "5.5/2.1 mm DC barrel jack",
            "Single-cell Li-ion pack with an on-board charger",
            "2-pin header fed from an external supply",
        ),
        stated_when=(
            "usb",
            "battery",
            "barrel",
            "dc jack",
            "adapter",
            "poe",
            "电池",
            "锂电",
            "适配器",
            "电源输入",
            "外部供电",
        ),
    ),
    Gap(
        slot="main_rail",
        question={
            "en": "Main supply rail — no voltage stated. What should the logic run on?",
            "zh": "主电源轨 —— 需求里没写电压。逻辑电平跑几伏？",
        },
        assumed="3.3 V main rail from a linear regulator",
        basis=_ENGINEERING_DEFAULT,
        alternatives=(
            "5 V rail with no regulator, when every device on the board accepts 5 V",
            "1.8 V main rail from a linear regulator",
            "3.3 V rail from a switching regulator",
        ),
        # Any explicit rail voltage counts as stated, however it is written.
        stated_pattern=r"\d(?:[.,]\d+)?\s*v(?:olt)?\b|\b[135]v[38]?\b|3\.3|1\.8",
        stated_when=("ldo", "稳压", "电压轨", "regulator"),
    ),
    Gap(
        slot="clock_source",
        question={
            "en": "Clock source — not stated. External crystal or the internal oscillator?",
            "zh": "时钟来源 —— 需求里没写。用外部晶振还是内部振荡器？",
        },
        # No number here on purpose: the required frequency is a datasheet fact
        # and belongs to the phase that can cite it.
        assumed=(
            "External crystal at the frequency the MCU datasheet requires, with the "
            "load capacitors that datasheet specifies"
        ),
        basis=_CONVENTION,
        alternatives=(
            "Internal oscillator only, with no external crystal",
            "External crystal plus a separate 32.768 kHz low-speed crystal",
        ),
        stated_when=(
            "crystal",
            "xtal",
            "oscillator",
            "mhz",
            "khz",
            "晶振",
            "晶体",
            "振荡",
            "内部时钟",
            "时钟源",
        ),
    ),
    Gap(
        slot="debug_port",
        question={
            "en": "Debug and programming — not stated. How is the MCU flashed?",
            "zh": "调试与烧写 —— 需求里没写。芯片怎么下载程序？",
        },
        assumed="4-pin debug header carrying data, clock, GND and the logic rail",
        basis=_CONVENTION,
        alternatives=(
            "No debug header; program over the MCU's built-in USB or serial bootloader",
            "10-pin Cortex debug connector",
            "Test pads only, no connector",
        ),
        stated_when=(
            "swd",
            "jtag",
            "debug",
            "bootloader",
            "icsp",
            "调试",
            "烧写",
            "下载口",
            "仿真",
        ),
    ),
    Gap(
        slot="led_resistor",
        question={
            "en": "Indicator LED series resistor — no value stated. Which value?",
            "zh": "指示灯限流电阻 —— 需求里没写阻值。用多大？",
        },
        assumed="1 kOhm series resistor per indicator LED",
        basis=_ENGINEERING_DEFAULT,
        alternatives=("330 Ohm series resistor", "2.2 kOhm series resistor"),
        applies_when=("led", "指示灯", "状态灯"),
        stated_pattern=r"\d+\s*(?:k\s*)?(?:ohm|ω|欧)|\b\d+\s*k\b",
        stated_when=("限流",),
    ),
    Gap(
        slot="mounting",
        question={
            "en": "Mechanical mounting — not stated. How is the board fixed down?",
            "zh": "机械固定 —— 需求里没写。板子怎么固定？",
        },
        assumed="Four non-plated M2 mounting holes, one at each corner",
        basis=_CONVENTION,
        alternatives=(
            "Four non-plated M3 mounting holes, one at each corner",
            "No mounting holes",
        ),
        stated_when=(
            "mounting hole",
            "standoff",
            "m2",
            "m3",
            "安装孔",
            "固定孔",
            "螺丝",
            "螺钉",
        ),
    ),
)


def requirement_gaps(
    requirement: str,
    language: str,
    *,
    settled: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Slots this requirement leaves open, in the shape assumptions are recorded in.

    The text is stripped of runtime-appended evidence first: fact sheets and
    symbol dumps mention voltages and package names the user never asked for, and
    reading those as user intent suppressed the very questions this exists to
    raise.
    """
    lowered = strip_runtime_evidence(requirement or "").lower()
    if not lowered.strip():
        return []
    return [
        gap.record(language)
        for gap in GAPS
        if gap.slot not in settled and gap.relevant(lowered) and not gap.stated(lowered)
    ]


def merge_assumptions(
    recorded: list[dict[str, Any]],
    derived: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Architect-recorded assumptions first, then derived gaps it did not cover.

    Recorded entries win on a shared slot: they were produced next to the fact
    sheets and can carry a citation, while a derived entry can only offer a
    convention.
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*recorded, *derived]:
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot") or "").strip()
        if not slot or slot in seen:
            continue
        seen.add(slot)
        merged.append(item)
    return merged
