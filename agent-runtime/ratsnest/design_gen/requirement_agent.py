"""Requirement Understanding Agent: natural language -> DesignSpec.

Brain-first: when an LLM is available it interprets the requirement (handles
complex, indirect, or multilingual phrasing) and must emit JSON that
validates against the DesignSpec contract — invalid output falls back to the
deterministic pattern extractor, and the run records which brain decided.
"""

from __future__ import annotations

import re

from ratsnest.protocols import LlmBrain
from ratsnest.schemas import DesignSpec

_SPEC_PROMPT = """You convert an electronics requirement into a DesignSpec \
JSON for a linear-regulator board generator (the only family supported: one \
adjustable LDO stepping an input rail down to an output rail, with an \
optional indicator LED).

Return ONLY a JSON object with exactly these keys:
  project_name       short snake_case slug derived from the requirement
  input_voltage      number (volts, must be greater than output_voltage)
  output_voltage     number (volts)
  output_current_a   number (amps, default 0.5 if unstated)
  led                one of "red","green","blue","yellow","white","orange" \
or null if the user does not want an LED

Rules: a linear regulator steps DOWN, so if roles are ambiguous the larger \
voltage is the input. Requirements may be in any language. If the user asks \
for anything beyond this family (buck, MCU, USB...), still map the power \
rails onto this family — the checker crew will flag mismatches later."""


def parse_requirement_llm(text: str, llm: LlmBrain) -> DesignSpec | None:
    """Brain path. Returns a validated DesignSpec or None (caller falls back)."""
    if llm is None or not llm.available:
        return None
    raw = llm.complete_json("requirement_agent", _SPEC_PROMPT,
                            f"Requirement: {text}", max_tokens=500)
    if not raw:
        return None
    try:
        raw.setdefault("requirement_text", text)
        if raw.get("project_name"):
            raw["project_name"] = re.sub(
                r"[^a-z0-9]+", "_", str(raw["project_name"]).lower()).strip("_")[:40]
        spec = DesignSpec.model_validate(raw)
    except Exception:
        return None
    # contract sanity gates — the brain proposes, the contract disposes
    if not (0 < spec.output_voltage < spec.input_voltage <= 60):
        return None
    if spec.led is not None and spec.led.lower() not in _LED_COLORS:
        spec.led = "red"
    if not spec.project_name:
        spec.project_name = "generated_board"
    return spec

_LED_COLORS = ("red", "green", "blue", "yellow", "white", "orange")

# role markers are position-aware: prepositions come BEFORE the number
# ("from 12V", "to 5V"), labels come AFTER it ("12V input", "5V output")
_IN_BEFORE = re.compile(r"\b(from|input|vin|supply)\s*$")
_OUT_BEFORE = re.compile(r"\b(to|into|output|vout)\s*$")
_IN_AFTER = re.compile(r"^\s*(input|in\b|supply)")
_OUT_AFTER = re.compile(r"^\s*(output|out\b|rail)")


def _classify_voltages(text: str) -> tuple[list[float], list[float], list[float]]:
    lower = text.lower()
    inputs, outputs, unknown = [], [], []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*v(?:olts?)?\b", lower):
        v = float(m.group(1))
        before = lower[max(0, m.start() - 16):m.start()]
        after = lower[m.end():m.end() + 12]
        if _IN_BEFORE.search(before) or _IN_AFTER.search(after):
            inputs.append(v)
        elif _OUT_BEFORE.search(before) or _OUT_AFTER.search(after):
            outputs.append(v)
        else:
            unknown.append(v)
    return inputs, outputs, unknown


def parse_requirement(text: str) -> DesignSpec:
    spec = DesignSpec(requirement_text=text)
    lower = text.lower()

    inputs, outputs, unknown = _classify_voltages(text)
    if inputs:
        spec.input_voltage = inputs[0]
    if outputs:
        spec.output_voltage = outputs[0]
    if unknown:
        if not inputs and not outputs and len(unknown) >= 2:
            # bare "12V ... 5V": a linear regulator steps down
            spec.input_voltage, spec.output_voltage = max(unknown), min(unknown)
        elif not outputs and len(unknown) == 1:
            spec.output_voltage = unknown[0]
        elif not inputs and len(unknown) == 1:
            spec.input_voltage = unknown[0]

    current = re.search(r"(\d+(?:\.\d+)?)\s*(m?)a\b", lower)
    if current:
        val = float(current.group(1))
        spec.output_current_a = val / 1000 if current.group(2) else val

    if re.search(r"\bno\s+led\b|\bwithout\s+(an?\s+)?led\b", lower):
        spec.led = None
    else:
        for color in _LED_COLORS:
            if color in lower:
                spec.led = color
                break

    slug = re.sub(r"[^a-z0-9]+", "_", lower).strip("_")[:40]
    if slug:
        spec.project_name = slug
    return spec
