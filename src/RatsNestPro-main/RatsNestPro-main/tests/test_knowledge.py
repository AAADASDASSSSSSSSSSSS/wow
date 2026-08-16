"""Task 9: retrieval knowledge base (lexical offline + fake embedder)."""

from __future__ import annotations

import re

from ratsnestpro.knowledge import Doc, KnowledgeBase, build_default_kb

_TOKEN = re.compile(r"[a-z0-9]+")
_VOCAB = [
    "decoupling", "capacitor", "crystal", "load", "ldo", "regulator",
    "reset", "voltage", "16mhz", "8mhz", "ground", "supply", "header",
]


class FakeEmbedder:
    """Deterministic bag-of-words embedder over a fixed vocabulary (no network)."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            toks = set(_TOKEN.findall(t.lower()))
            out.append([1.0 if v in toks else 0.0 for v in _VOCAB])
        return out


def test_default_kb_loads_corpus() -> None:
    kb = build_default_kb()
    assert len(kb.docs) >= 5


def test_lexical_retrieval_finds_relevant_doc() -> None:
    kb = build_default_kb()
    hits = kb.retrieve("crystal load capacitor for a 16 MHz oscillator", top_k=3)
    assert hits
    assert hits[0].doc.id == "crystal"


def test_lexical_retrieval_decoupling() -> None:
    kb = build_default_kb()
    hits = kb.retrieve("how many decoupling capacitors and where to place them", top_k=3)
    assert hits[0].doc.id == "decoupling"


def test_role_filtering() -> None:
    kb = KnowledgeBase()
    kb.add([
        Doc(id="only_repair", text="repair strategy for decoupling", role="repair"),
        Doc(id="only_arch", text="architecture pattern for headers", role="architect"),
    ])
    hits = kb.retrieve("decoupling", role="repair")
    ids = {h.doc.id for h in hits}
    assert "only_repair" in ids
    assert "only_arch" not in ids


def test_fake_embedder_path() -> None:
    kb = build_default_kb(embedder=FakeEmbedder())
    hits = kb.retrieve("decoupling capacitor", top_k=3)
    # The crude fixed-vocab FakeEmbedder cannot break ties between docs that
    # share the same query words, so assert the decoupling doc is among the top
    # results (robust to corpus growth) rather than strictly rank 1.
    assert hits and "decoupling" in {h.doc.id for h in hits}


def test_graceful_degradation_without_embedder() -> None:
    # No embedder, no network — lexical retrieval still returns results.
    kb = build_default_kb(embedder=None)
    assert kb.retrieve("ldo regulator output capacitor")


def test_retrieve_text_snippet() -> None:
    kb = build_default_kb()
    text = kb.retrieve_text("reset pull-up resistor", top_k=1)
    assert "reset" in text.lower()
