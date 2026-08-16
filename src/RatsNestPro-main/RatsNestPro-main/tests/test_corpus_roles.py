"""Task 3: expanded PCB-design corpus, indexed and retrievable by pipeline role."""

from __future__ import annotations

from ratsnestpro.knowledge import build_default_kb

# Pipeline-stage role tags introduced by the expanded corpus.
_STAGE_ROLES = {
    "topology",
    "selection",
    "schematic",
    "layout",
    "routing",
    "stackup",
    "dfm",
    "emc",
}


def test_corpus_grew_and_all_docs_have_roles() -> None:
    kb = build_default_kb()
    # Original 5 + 11 new design-knowledge files.
    assert len(kb.docs) >= 15
    for doc in kb.docs:
        assert doc.roles(), f"{doc.id} has no role tag"


def test_pipeline_stage_roles_present() -> None:
    kb = build_default_kb()
    seen: set[str] = set()
    for doc in kb.docs:
        seen.update(doc.roles())
    # Every pipeline stage role must be represented by at least one doc.
    assert _STAGE_ROLES <= seen, f"missing roles: {_STAGE_ROLES - seen}"


def test_routing_role_retrieval() -> None:
    kb = build_default_kb()
    hits = kb.retrieve("what trace width for 1 amp current capacity", top_k=3, role="routing")
    ids = [h.doc.id for h in hits]
    assert "trace_width_current" in ids


def test_layout_role_retrieval() -> None:
    kb = build_default_kb()
    hits = kb.retrieve(
        "how to partition placement into functional zones", top_k=3, role="layout"
    )
    ids = [h.doc.id for h in hits]
    assert any(i in ids for i in ("placement_partitioning", "placement_constraints"))


def test_role_filter_excludes_off_stage_docs() -> None:
    kb = build_default_kb()
    # A routing query filtered to layout must not surface the routing-only doc.
    hits = kb.retrieve("trace width current", top_k=5, role="layout")
    assert "trace_width_current" not in {h.doc.id for h in hits}


def test_retrieve_text_for_stage() -> None:
    kb = build_default_kb()
    text = kb.retrieve_text("stackup and controlled impedance", top_k=1, role="stackup")
    assert "impedance" in text.lower() or "stackup" in text.lower()
