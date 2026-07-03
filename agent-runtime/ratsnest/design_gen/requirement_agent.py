"""Requirement agent: natural language -> DesignSpec.

Deterministic pattern extraction (default, zero API keys). An LLM hook can
replace this for richer requirements later; the output contract (DesignSpec)
stays identical either way.
"""

from __future__ import annotations

import re

from ratsnest.schemas import DesignSpec

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
