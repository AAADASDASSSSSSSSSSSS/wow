"""Task 1: symbol library adapter — new .kicad_symdir format + cross-file extends.

These tests build tiny fixture libraries in both on-disk layouts so they run
fully offline without the real KiCad libraries installed. KICAD_SYMBOL_DIR is
pointed at the fixture root via monkeypatch.
"""

from __future__ import annotations

import pytest

from ratsnestpro.eda import grounding, symbols


@pytest.fixture(autouse=True)
def _clear_cache():
    # The file parse cache is keyed by path; clear around each test so fixtures
    # written to fresh tmp dirs are always re-read.
    symbols._load_lib_node.cache_clear()
    yield
    symbols._load_lib_node.cache_clear()


def _legacy_lib(text_symbols: str) -> str:
    return (
        '(kicad_symbol_lib (version 20231120) (generator "test")\n'
        f"{text_symbols}\n)"
    )


def _pin(number: str, name: str, etype: str, x: float, y: float, angle: float) -> str:
    return (
        f'(pin {etype} line (at {x} {y} {angle}) (length 2.54) '
        f'(name "{name}") (number "{number}"))'
    )


# --- library path resolution --------------------------------------------- #


def test_symbol_roots_prefers_configured_paths(tmp_path, monkeypatch) -> None:
    """A configured directory that exists is the only root consulted."""
    configured = tmp_path / "configured"
    configured.mkdir()
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(configured))
    assert symbols.symbol_roots() == [configured]


def test_symbol_roots_falls_back_when_configured_paths_are_absent(
    tmp_path, monkeypatch
) -> None:
    """A stale env value must not silence the resolver.

    ``KICAD_SYMBOL_DIR`` pointing at a moved or deleted directory used to make
    this return an empty list, because discovery ran only when the variable was
    unset. Nothing raised: every symbol lookup returned ``None``, so pin
    geometry, alternate functions and pad counts stopped being verified while
    ``config.symbol_dir()`` — which does fall back — kept reporting the library
    as available. Same question, two answers.
    """
    discovered = tmp_path / "discovered"
    discovered.mkdir()
    monkeypatch.setattr(
        "ratsnestpro.eda.vendor.symbol_lib.symbol_roots", lambda: [discovered]
    )
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path / "deleted-long-ago"))
    assert symbols.symbol_roots() == [discovered]

    # Unset behaves the same way, which is what makes the two paths one rule.
    monkeypatch.delenv("KICAD_SYMBOL_DIR")
    assert symbols.symbol_roots() == [discovered]


def test_legacy_single_file_format(tmp_path, monkeypatch) -> None:
    # Old layout: Device.kicad_sym holds R and C as separate symbols.
    r_pins = _pin("1", "~", "passive", 0, 3.81, 270) + _pin("2", "~", "passive", 0, -3.81, 90)
    lib = _legacy_lib(
        f'(symbol "R" (symbol "R_1_1" {r_pins}))\n'
        f'(symbol "C" (symbol "C_1_1" '
        f'{_pin("1", "~", "passive", 0, 2.54, 270)}{_pin("2", "~", "passive", 0, -2.54, 90)}))'
    )
    (tmp_path / "Device.kicad_sym").write_text(lib, encoding="utf-8")
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path))

    r = symbols.symbol_pins("Device:R")
    assert r is not None
    assert {p["number"] for p in r} == {"1", "2"}
    c = symbols.symbol_pins("Device:C")
    assert c is not None and len(c) == 2


def test_explicit_symbol_root_does_not_append_discovered_system_roots(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path))
    monkeypatch.setattr(
        "ratsnestpro.eda.vendor.symbol_lib.symbol_roots",
        lambda: pytest.fail("system symbol discovery must not run"),
    )

    assert symbols.symbol_roots() == [tmp_path]


def test_symbol_index_does_not_fill_the_full_symbol_parse_cache(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "Device.kicad_sym").write_text(
        _legacy_lib(
            f'(symbol "R" (symbol "R_1_1" '
            f'{_pin("1", "~", "passive", 0, 3.81, 270)}'
            f'{_pin("2", "~", "passive", 0, -3.81, 90)}))'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path))
    grounding._symbol_index_for.cache_clear()
    monkeypatch.setattr(
        symbols,
        "_load_lib_node",
        lambda _path: pytest.fail("symbol index used the retained parse cache"),
    )

    assert grounding.symbol_index() == ("Device:R",)


def test_new_symdir_format(tmp_path, monkeypatch) -> None:
    # New layout: Device.kicad_symdir/ with one file per symbol.
    symdir = tmp_path / "Device.kicad_symdir"
    symdir.mkdir()
    r_pins = _pin("1", "~", "passive", 0, 3.81, 270) + _pin("2", "~", "passive", 0, -3.81, 90)
    (symdir / "R.kicad_sym").write_text(
        _legacy_lib(f'(symbol "R" (symbol "R_1_1" {r_pins}))'), encoding="utf-8"
    )
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path))

    pins = symbols.symbol_pins("Device:R")
    assert pins is not None
    assert {p["number"] for p in pins} == {"1", "2"}
    # resolve_symbol points at the per-symbol file inside the .kicad_symdir.
    resolved = symbols.resolve_symbol("Device:R")
    assert resolved is not None and resolved.name == "R.kicad_sym"


def test_cross_file_extends_inheritance(tmp_path, monkeypatch) -> None:
    # Derived symbol carries no pins; base lives in a sibling file (new format).
    symdir = tmp_path / "MCU_Microchip_ATmega.kicad_symdir"
    symdir.mkdir()
    base_pins = (
        _pin("4", "VCC", "power_in", -10, 5, 0)
        + _pin("3", "GND", "power_in", -10, -5, 0)
        + _pin("1", "PB0", "bidirectional", 10, 5, 180)
    )
    (symdir / "ATmega48PV-10A.kicad_sym").write_text(
        _legacy_lib(f'(symbol "ATmega48PV-10A" (symbol "ATmega48PV-10A_1_1" {base_pins}))'),
        encoding="utf-8",
    )
    # Derived part: only (extends ...), no pins of its own.
    (symdir / "ATmega328P-A.kicad_sym").write_text(
        _legacy_lib('(symbol "ATmega328P-A" (extends "ATmega48PV-10A"))'),
        encoding="utf-8",
    )
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path))

    pins = symbols.symbol_pins("MCU_Microchip_ATmega:ATmega328P-A")
    assert pins is not None
    numbers = {p["number"] for p in pins}
    assert {"1", "3", "4"} <= numbers
    vcc = next(p for p in pins if p["number"] == "4")
    assert vcc["name"] == "VCC"


def test_unknown_symbol_returns_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path))
    assert symbols.symbol_pins("Device:DoesNotExist") is None
    assert symbols.resolve_symbol("Device:DoesNotExist") is None
    assert symbols.symbol_pins("NoColon") is None


def test_symbol_info_shape(tmp_path, monkeypatch) -> None:
    symdir = tmp_path / "Device.kicad_symdir"
    symdir.mkdir()
    (symdir / "R.kicad_sym").write_text(
        _legacy_lib(
            f'(symbol "R" (symbol "R_1_1" '
            f'{_pin("1", "~", "passive", 0, 3.81, 270)}'
            f'{_pin("2", "~", "passive", 0, -3.81, 90)}))'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path))
    info = symbols.symbol_info("Device:R")
    assert info is not None
    assert info["pin_count"] == 2
    assert info["lib_id"] == "Device:R"
    assert info["path"] is not None
