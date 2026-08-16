"""The Architect must not ask for evidence this checkout already holds.

A real run stalled by listing seven "missing evidence" items — HSE pin numbers, a
USB-C CC pulldown, the AMS1117's output capacitor, decoupling placement — and
asking the user to supply datasheet pages. Every one of those values was already
in the repository: the pin numbers in the installed KiCad symbol, the rest in
``data/fact_sheets``. The Architect never saw them because its evidence payload
carried only the primary MCU's symbol plus a web search.

These tests pin the assembly, not the model: the values must be present in what
gets handed to it, so an "evidence gap" in the reply is a model failure rather
than a missing source.
"""

from __future__ import annotations

import pytest

from agents.ratsnestpro import local_evidence

_REQUIREMENT = (
    "STM32F103C8T6 minimal board, 50x40mm two-layer. A USB-C connector feeds an "
    "AMS1117-3.3 regulator. 8MHz crystal with 20pF load capacitors, NRST button with "
    "a 10k pullup, PC13 status LED, 4-pin SWD header, M2 mounting holes."
)


@pytest.fixture(autouse=True)
def _lexical_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rank the corpus locally and never share a cached base between tests.

    Embedded retrieval is a gateway call; asserting on its ranking would make
    these tests depend on a network. The corpus read is identical either way.
    """
    monkeypatch.delenv("RATSNESTPRO_KB_RETRIEVAL", raising=False)
    monkeypatch.setattr(local_evidence, "_KB", None)
    monkeypatch.setattr(local_evidence, "_KB_MODE", "")


def _all_brief_text(payload: dict) -> str:
    facts = payload["fact_sheets"]
    blocks = list(facts["by_step"].values())
    blocks.extend(facts.get("candidate_by_step", {}).values())
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Fact sheets — the values the stalled run asked for
# --------------------------------------------------------------------------- #


def test_named_devices_resolve_from_the_requirement_text() -> None:
    payload = local_evidence.collect(_REQUIREMENT)
    devices = payload["fact_sheets"]["devices"]
    assert "STM32F103" in devices
    assert "AMS1117-3.3" in devices


def test_regulator_output_capacitor_survives_the_budget() -> None:
    """The MCU sheet must not crowd out a mandatory capacitor.

    ``factbrief``'s own 2_400-character default dropped this slot: the MCU is
    rendered first and lower-consequence facts leave first, so the regulator's
    ``required_cout`` was announced as omitted. Announced-but-absent is still
    absent as far as the model is concerned.
    """
    payload = local_evidence.collect(_REQUIREMENT)
    text = _all_brief_text(payload)
    assert "required_cout" in text
    assert "22 uF" in text


def test_usb_c_cc_pulldown_reaches_the_prompt_without_an_order_code() -> None:
    """A generic "USB-C connector" must still carry the class-level Rd rule.

    ``fact_sheets_named`` matches the connector sheet only through its LCSC code,
    and a requirement never states one. Without the class fallback the single
    most common reason a first USB-C board never powers up is invisible here.
    """
    payload = local_evidence.collect(_REQUIREMENT)
    assert "USB-C 16P" in payload["fact_sheets"]["candidate_devices"]
    text = _all_brief_text(payload)
    assert "cc_pulldown_ohm" in text
    assert "5100" in text


def test_decoupling_and_load_capacitance_are_present() -> None:
    payload = local_evidence.collect(_REQUIREMENT)
    text = _all_brief_text(payload)
    assert "decoupling" in text
    assert "load_capacitance_pf" in text


def test_class_candidates_are_labelled_as_not_yet_selected() -> None:
    payload = local_evidence.collect(_REQUIREMENT)
    note = payload["fact_sheets"]["candidate_note"]
    assert "no sheet matched by name" in note


# --------------------------------------------------------------------------- #
# Conventions — an unstated value becomes an assumption, not a question
# --------------------------------------------------------------------------- #


def test_board_level_conventions_cover_the_unstated_values() -> None:
    topics = {item["topic"] for item in local_evidence.conventions_for(_REQUIREMENT)}
    assert "two-layer stackup" in topics
    assert "signal track width" in topics
    assert "crystal routing" in topics
    assert "decoupling placement" in topics


def test_conventions_are_never_attributed_to_a_datasheet() -> None:
    for item in local_evidence.conventions_for(_REQUIREMENT):
        assert item["source"] == "engineering convention, not a datasheet limit"
        assert item["why"]


def test_conditional_conventions_stay_out_when_irrelevant() -> None:
    topics = {item["topic"] for item in local_evidence.conventions_for("A four-layer ESP32 board")}
    assert "two-layer stackup" not in topics
    assert "crystal routing" not in topics
    # Unconditional ones still apply.
    assert "signal track width" in topics


# --------------------------------------------------------------------------- #
# Process capability and soft knowledge
# --------------------------------------------------------------------------- #


def test_process_capability_travels_with_the_evidence() -> None:
    capability = local_evidence.collect(_REQUIREMENT)["process_capability"]
    assert capability["min_track_width"] > 0
    assert capability["min_clearance"] > 0


def test_corpus_retrieval_returns_passages_and_names_its_mode() -> None:
    practice = local_evidence.design_practice("two layer stackup and trace width", top_k=3)
    assert practice["passages"]
    assert practice["retrieval"]
    assert all(item["id"] for item in practice["passages"])


def test_corpus_passages_are_marked_advisory() -> None:
    practice = local_evidence.design_practice(_REQUIREMENT, top_k=2)
    assert "Never cite these as a limit" in practice["advisory"]


# --------------------------------------------------------------------------- #
# Observability — a run must show whether local data was consulted
# --------------------------------------------------------------------------- #


def test_coverage_reports_what_was_consulted() -> None:
    payload = local_evidence.collect(_REQUIREMENT)
    coverage = local_evidence.coverage(payload)
    assert "STM32F103" in coverage["fact_sheet_devices"]
    assert "USB-C 16P" in coverage["fact_sheet_candidates"]
    assert coverage["fact_sheet_steps"]
    assert coverage["corpus_retrieval"]
    assert coverage["corpus_docs"]
    assert coverage["conventions"] > 0


def test_coverage_tolerates_an_empty_payload() -> None:
    assert local_evidence.coverage({})["conventions"] == 0
