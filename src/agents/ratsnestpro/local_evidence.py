"""Everything this checkout can answer about a requirement without a network.

The Architect used to be handed one grounded document — the primary MCU's KiCad
symbol — plus whatever a web search returned. Behind a proxy that blocks the
search it saw only the symbol, and answered by asking the user for datasheet
pages that this repository already ships as curated fact sheets. That reads as an
evidence gap and is not one, and it stalls the run on material the agent is
holding.

Three sources are assembled here, in descending authority:

* ``fact_sheets`` — curated, cited datasheet slots for every device the
  requirement names (``ratsnestpro.eda.factbrief``). Hard facts.
* ``design_practice`` — the soft-knowledge corpus, retrieved. Advisory only;
  ``ratsnestpro.knowledge`` is explicit that retrieved text is never a fact.
* ``conventions`` — board-level choices no datasheet states, each carrying its
  rationale and the fab capability it must clear. These exist so an unstated
  value becomes a labelled assumption instead of a question to the user.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ratsnestpro.config import process_capability
from ratsnestpro.eda import factbrief
from ratsnestpro.eda.factsheet import DeviceClass, FactSheetBase, all_fact_sheets
from ratsnestpro.knowledge import KnowledgeBase, build_default_kb

# A two-device design renders to about 2.6k characters per step and saturates
# there. 2_400 — the module default in factbrief — silently dropped the AMS1117's
# mandatory output capacitor because the MCU sheet is rendered first and consumed
# the whole allowance, which is exactly the "asked for evidence it was holding"
# failure this module exists to stop.
_MAX_STEP_BRIEF_CHARS = 6_000
_MAX_PRACTICE_CHARS = 1_200


@dataclass(frozen=True)
class Convention:
    """A board-level default with its reason, and what makes it applicable.

    ``applies_when`` holds lowercase substrings of the requirement. An empty
    tuple means the convention is unconditional for any board.
    """

    topic: str
    value: str
    rationale: str
    applies_when: tuple[str, ...] = ()

    def applicable(self, requirement: str) -> bool:
        if not self.applies_when:
            return True
        lowered = requirement.lower()
        return any(token in lowered for token in self.applies_when)

    def payload(self) -> dict[str, str]:
        return {
            "topic": self.topic,
            "default": self.value,
            "why": self.rationale,
            "source": "engineering convention, not a datasheet limit",
        }


# Numbers here are either derived from ``data/process_capability.json`` at render
# time or are the conventional working values that clear it with margin. Nothing
# in this table is attributed to a datasheet, because no datasheet states it.
CONVENTIONS: tuple[Convention, ...] = (
    Convention(
        topic="two-layer stackup",
        value="Bottom is an uninterrupted GND plane; Top carries signals plus a poured power area",
        rationale=(
            "Every return current needs a continuous reference under its outgoing trace; "
            "splitting the only ground layer on a two-layer board forces long return loops"
        ),
        applies_when=("two layer", "two-layer", "2 layer", "2-layer", "两层", "双层"),
    ),
    Convention(
        topic="signal track width",
        value="0.25 mm",
        rationale=(
            "Roughly double the fab minimum track width, so etch tolerance cannot open a "
            "trace, while staying routable between 0.5 mm-pitch QFP pads"
        ),
    ),
    Convention(
        topic="power track width",
        value="0.5 mm for rails under 1 A",
        rationale=(
            "Keeps IR drop and temperature rise negligible at these currents without "
            "consuming routing channels; widen from a current calculation above 1 A"
        ),
    ),
    Convention(
        topic="track clearance",
        value="at or above the fab minimum clearance",
        rationale="Below the fab minimum the board is not manufacturable, not merely marginal",
    ),
    Convention(
        topic="decoupling placement",
        value="Within about 2 mm of the pin it serves, same side as the device, own ground via",
        rationale=(
            "The loop inductance the capacitor is there to cancel scales with the loop it "
            "closes; a long via or a shared return undoes it"
        ),
    ),
    Convention(
        topic="crystal routing",
        value=(
            "OSC_IN and OSC_OUT short and length-matched, guard ground either side, "
            "unbroken ground under both traces, load capacitors grounded at the crystal"
        ),
        rationale=(
            "The oscillator is a high-impedance node: stray capacitance pulls frequency and "
            "injected noise stops start-up"
        ),
        applies_when=("crystal", "xtal", "osc", "mhz", "晶振", "晶体"),
    ),
    Convention(
        topic="mounting holes",
        value="Non-plated, keep-out equal to the screw head, no copper or traces beneath",
        rationale="A plated hole under a metal standoff shorts whatever the pour carries",
        applies_when=("mounting hole", "m2", "m3", "安装孔", "螺丝"),
    ),
    Convention(
        topic="unanswered strap pin",
        value="Resolve to the boot-from-internal-flash state with a 10 kΩ resistor",
        rationale=(
            "A floating strap pin samples noise at reset, so the state must be forced; the "
            "flash-boot state is the one a finished board needs"
        ),
        applies_when=("boot", "strap", "启动"),
    ),
)


def conventions_for(requirement: str) -> list[dict[str, str]]:
    """Board-level defaults that apply to this requirement."""
    return [item.payload() for item in CONVENTIONS if item.applicable(requirement)]


# --------------------------------------------------------------------------- #
# Soft-knowledge corpus
# --------------------------------------------------------------------------- #

_KB: KnowledgeBase | None = None
_KB_MODE = ""


def _embedded_retrieval_wanted() -> bool:
    """Whether to embed the corpus with EricAI rather than score it lexically.

    ``build_default_kb()`` was being called with no embedder anywhere in the tree,
    so ``EricAIEmbedder`` and ``EricAIReranker`` existed but never ran. They are
    reachable now, but off by default and opted into with
    ``RATSNESTPRO_KB_RETRIEVAL=ericai``: embedding all 46 corpus documents is a
    network round trip, and a default that quietly makes one turns every test run
    and every offline run into a gateway dependency. Both paths read the same
    corpus, so the local data is used either way — only the ranking differs.
    """
    return os.getenv("RATSNESTPRO_KB_RETRIEVAL", "").strip().lower() in {
        "ericai",
        "embedded",
        "1",
        "true",
    }


def _knowledge_base() -> tuple[KnowledgeBase, str]:
    global _KB, _KB_MODE
    if _KB is None:
        if _embedded_retrieval_wanted():
            from ratsnestpro.knowledge import EricAIEmbedder, EricAIReranker

            _KB = build_default_kb(embedder=EricAIEmbedder(), reranker=EricAIReranker())
            _KB_MODE = "ericai-bge-m3+reranker"
        else:
            _KB = build_default_kb()
            _KB_MODE = "lexical"
    return _KB, _KB_MODE


def _reset_to_lexical() -> tuple[KnowledgeBase, str]:
    global _KB, _KB_MODE
    _KB = build_default_kb()
    _KB_MODE = "lexical (embedding unavailable)"
    return _KB, _KB_MODE


def design_practice(requirement: str, top_k: int = 4) -> dict[str, Any]:
    """Top corpus passages for this requirement, with the mode that ranked them.

    Embedding is a network call. A gateway failure must degrade to the
    dependency-free lexical score rather than take the phase down, so the first
    failure rebuilds the base without an embedder and retries once.
    """
    kb, mode = _knowledge_base()
    try:
        hits = kb.retrieve(requirement, top_k=top_k)
    except Exception:
        kb, mode = _reset_to_lexical()
        hits = kb.retrieve(requirement, top_k=top_k)
    return {
        "retrieval": mode,
        "advisory": "Design patterns, not datasheet facts. Never cite these as a limit.",
        "passages": [
            {
                "id": hit.doc.id,
                "score": round(hit.score, 4),
                "text": hit.doc.text.strip()[:_MAX_PRACTICE_CHARS],
            }
            for hit in hits
        ],
    }


# --------------------------------------------------------------------------- #
# Device classes named generically
# --------------------------------------------------------------------------- #

# A requirement says "USB-C connector", not "SHOU HAN TYPE-C 16PIN 2MD(073)", so
# ``fact_sheets_named`` resolves nothing and the class-level rules that decide
# whether the board powers up at all — the sink-side CC pulldown above all —
# never reach the model. Selection assigns the order code later; architecting
# still has to know the rule now.
_CLASS_KEYWORDS: dict[DeviceClass, tuple[str, ...]] = {
    DeviceClass.CONNECTOR: ("usb-c", "usb c", "type-c", "typec", "type c"),
    DeviceClass.CRYSTAL: ("crystal", "xtal", "晶振", "晶体"),
    DeviceClass.TVS: ("esd", "tvs", "静电"),
    DeviceClass.LDO: ("ldo", "linear regulator", "线性稳压", "稳压器"),
    DeviceClass.DCDC: ("buck", "boost", "switching regulator", "开关电源"),
}


def _class_candidates(requirement: str) -> list[FactSheetBase]:
    """Covered sheets for device classes the requirement names only generically."""
    lowered = requirement.lower()
    wanted = {
        device_class
        for device_class, keywords in _CLASS_KEYWORDS.items()
        if any(keyword in lowered for keyword in keywords)
    }
    if not wanted:
        return []
    return [sheet for sheet in all_fact_sheets() if sheet.device_class in wanted]


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def fact_sheet_brief(requirement: str, budget: int = _MAX_STEP_BRIEF_CHARS) -> dict[str, Any]:
    """Cited datasheet slots for every device the requirement names.

    Rendered per briefed step because ``SlotSpec.consumers`` routes slots to the
    step that consumes them: the supply range reaches selection, the decoupling
    rule reaches connections. Architecting spans all of them, so all of them are
    rendered instead of picking one and losing the rest.
    """
    entries = factbrief.sheets_mentioned(requirement)
    named = {sheet.device for _, sheet in entries}
    candidates = [
        ("", sheet) for sheet in _class_candidates(requirement) if sheet.device not in named
    ]
    blocks: dict[str, str] = {}
    candidate_blocks: dict[str, str] = {}
    for step in sorted(factbrief.BRIEFED_STEPS):
        text = factbrief.brief(step, entries, budget=budget)
        if text:
            blocks[step] = text
        if candidates:
            candidate_text = factbrief.brief(step, candidates, budget=budget)
            if candidate_text:
                candidate_blocks[step] = candidate_text
    payload: dict[str, Any] = {
        "devices": [sheet.device for _, sheet in entries],
        "by_step": blocks,
        "authority": (
            "Curated datasheet extracts with citations. Authoritative. A slot marked "
            "NOT STATED is unknown, not unlimited."
        ),
    }
    if candidate_blocks:
        payload["candidate_devices"] = [sheet.device for _, sheet in candidates]
        payload["candidate_by_step"] = candidate_blocks
        payload["candidate_note"] = (
            "The requirement names these device CLASSES generically without an order "
            "code, so no sheet matched by name. These are the covered parts of that "
            "class in this checkout: usable as the design basis and as the selection "
            "shortlist. Their class-level rules (a sink-side CC pulldown, a mandatory "
            "output capacitor) hold whichever part of the class is finally chosen."
        )
    return payload


def collect(requirement: str) -> dict[str, Any]:
    """Every locally answerable source for one requirement, in one payload."""
    capability = process_capability()
    return {
        "fact_sheets": fact_sheet_brief(requirement),
        "design_practice": design_practice(requirement),
        "process_capability": capability.model_dump(),
        "conventions": conventions_for(requirement),
    }


def coverage(payload: dict[str, Any]) -> dict[str, Any]:
    """Counts for the trace, so a run shows whether local data was consulted."""
    facts = payload.get("fact_sheets", {}) if isinstance(payload, dict) else {}
    practice = payload.get("design_practice", {}) if isinstance(payload, dict) else {}
    return {
        "fact_sheet_devices": list(facts.get("devices", [])),
        "fact_sheet_candidates": list(facts.get("candidate_devices", [])),
        "fact_sheet_steps": sorted(facts.get("by_step", {})),
        "corpus_retrieval": practice.get("retrieval", ""),
        "corpus_docs": [item.get("id", "") for item in practice.get("passages", [])],
        "conventions": len(payload.get("conventions", [])) if isinstance(payload, dict) else 0,
    }
