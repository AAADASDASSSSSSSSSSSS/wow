"""Cross-platform KiCad installation path discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from ratsnestpro.eda.vendor import kicad_paths


@pytest.mark.parametrize(
    "relative",
    [
        Path("share/kicad/symbols"),
        Path("SharedSupport/symbols"),
        Path("SharedSupport/kicad/symbols"),
        Path("Resources/share/kicad/symbols"),
    ],
)
def test_symbol_dirs_support_common_install_layouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: Path,
) -> None:
    root = tmp_path / "install"
    expected = root / relative
    expected.mkdir(parents=True)
    monkeypatch.setattr(kicad_paths, "_install_roots", lambda: [root])

    assert kicad_paths.symbol_dirs() == [expected]


@pytest.mark.parametrize(
    "relative",
    [
        Path("share/kicad/footprints"),
        Path("SharedSupport/footprints"),
        Path("SharedSupport/kicad/footprints"),
        Path("Resources/share/kicad/footprints"),
    ],
)
def test_footprint_dirs_support_common_install_layouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: Path,
) -> None:
    root = tmp_path / "install"
    expected = root / relative
    expected.mkdir(parents=True)
    monkeypatch.setattr(kicad_paths, "_install_roots", lambda: [root])

    assert kicad_paths.footprint_dirs() == [expected]


def test_share_dirs_keep_root_order_and_remove_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_symbols = first / "share/kicad/symbols"
    second_symbols = second / "SharedSupport/symbols"
    first_symbols.mkdir(parents=True)
    second_symbols.mkdir(parents=True)
    monkeypatch.setattr(kicad_paths, "_install_roots", lambda: [first, second, first])

    assert kicad_paths.symbol_dirs() == [first_symbols, second_symbols]
