"""Task 11 (phase-3 slice): grounded part selection over the JLCPCB cache."""

from __future__ import annotations

import sqlite3

import pytest

from ratsnestpro.families import Atmega328Params, build_ir
from ratsnestpro.parts import PartSelector


@pytest.fixture
def cache_home(tmp_path, monkeypatch):
    """Point the vendored jlcpcb cache at a temp DB and seed a few parts."""
    monkeypatch.setenv("KICAD_MCP_HOME", str(tmp_path))
    db = tmp_path / "jlcpcb.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE parts (lcsc TEXT PRIMARY KEY, mpn TEXT, description TEXT,
            package TEXT, category TEXT, value TEXT, stock INTEGER, price REAL,
            datasheet TEXT, basic INTEGER);
        """
    )
    conn.executemany(
        "INSERT INTO parts VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("C1001", "RC0603FR-0710KL", "10k 1% 0603 resistor", "0603",
             "Resistors", "10k", 100000, 0.001, "", 1),
            ("C1002", "CL10B104KB8NNNC", "100nF 0603 X7R cap", "0603",
             "Capacitors", "100nF", 500000, 0.002, "", 1),
            ("C1003", "AP2112K-3.3TRG1", "3.3V LDO SOT-23-5", "SOT-23-5",
             "Regulators", "AP2112K-3.3", 20000, 0.08, "", 0),
        ],
    )
    conn.commit()
    conn.close()
    return tmp_path


def test_selector_unavailable_without_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KICAD_MCP_HOME", str(tmp_path / "empty"))
    sel = PartSelector()
    assert sel.available() is False
    assert sel.ground_ir(build_ir(Atmega328Params())) == {}


def test_search_returns_seeded_parts(cache_home) -> None:
    sel = PartSelector()
    assert sel.available() is True
    hits = sel.search("10k", limit=5)
    assert any(h.mpn == "RC0603FR-0710KL" for h in hits)


def test_suggest_by_value_and_package(cache_home) -> None:
    sel = PartSelector()
    cands = sel.suggest("100nF", "Capacitor_SMD:C_0603_1608Metric")
    assert cands and cands[0].package == "0603"
    assert cands[0].basic is True


def test_ground_ir_annotates_components(cache_home) -> None:
    sel = PartSelector()
    grounded = sel.ground_ir(build_ir(Atmega328Params()), limit=2)
    # At least the 100nF decouplers and 10k pull-up should get candidates.
    assert grounded, "expected some grounded candidates"
    assert all(isinstance(v, list) and v for v in grounded.values())


def test_cli_parts(cache_home, capsys) -> None:
    from ratsnestpro.cli import main

    rc = main(["parts", "10k"])
    assert rc == 0
    assert "RC0603FR-0710KL" in capsys.readouterr().out
