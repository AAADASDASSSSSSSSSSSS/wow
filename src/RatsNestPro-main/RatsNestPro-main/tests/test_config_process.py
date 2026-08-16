"""Task 2: library-path config + footprint pads + process capability table."""

from __future__ import annotations

import os

import pytest

from ratsnestpro import config
from ratsnestpro.eda import footprints


@pytest.fixture(autouse=True)
def _reset_config_state(monkeypatch):
    # Force .env re-evaluation and clear the capability cache each test.
    monkeypatch.setattr(config, "_ENV_LOADED", True)  # skip auto .env load in tests
    config._load_capability.cache_clear()
    yield
    config._load_capability.cache_clear()


# --- process capability --------------------------------------------------- #


def test_default_process_capability_loads() -> None:
    cap = config.process_capability()
    assert cap.min_track_width > 0
    assert cap.min_clearance > 0
    assert cap.min_via_drill < cap.min_via_diameter
    assert 2 in cap.layer_options


def test_process_capability_override(tmp_path, monkeypatch) -> None:
    custom = tmp_path / "cap.json"
    custom.write_text(
        '{"fab_house":"MyFab","min_track_width":0.2,"min_clearance":0.2,'
        '"min_via_diameter":0.6,"min_via_drill":0.3,"min_annular_ring":0.1,'
        '"min_hole_diameter":0.3,"min_board_edge_clearance":0.3}',
        encoding="utf-8",
    )
    monkeypatch.setenv("RATSNESTPRO_PROCESS_CAPABILITY", str(custom))
    cap = config.process_capability()
    assert cap.fab_house == "MyFab"
    assert cap.min_track_width == 0.2


# --- dotenv parsing ------------------------------------------------------- #


def test_load_dotenv_sets_env_without_override(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        'KICAD_SYMBOL_DIR="C:\\libs\\symbols"\nKICAD_FOOTPRINT_DIR=C:\\libs\\fp\n# comment\n',
        encoding="utf-8",
    )
    # No monkeypatch for these two variables, deliberately. ``load_dotenv``
    # writes into ``os.environ`` itself, and monkeypatch restores a variable to
    # whatever it held when monkeypatch was first asked to change it — which,
    # once ``load_dotenv`` has run, is the value ``load_dotenv`` wrote. Undo
    # therefore reinstates the dotenv value instead of removing it, and this
    # test used to leave ``KICAD_SYMBOL_DIR=C:\libs\symbols`` in place for the
    # rest of the session. Both library resolvers then pointed at a directory
    # that does not exist. Snapshot and restore by hand.
    keys = ("KICAD_SYMBOL_DIR", "KICAD_FOOTPRINT_DIR")
    saved = {key: os.environ.get(key) for key in keys}
    try:
        os.environ.pop("KICAD_SYMBOL_DIR", None)
        parsed = config.load_dotenv(env)
        assert parsed["KICAD_SYMBOL_DIR"] == "C:\\libs\\symbols"

        # Existing env wins unless override=True.
        os.environ["KICAD_SYMBOL_DIR"] = "already-set"
        config.load_dotenv(env)
        assert os.environ["KICAD_SYMBOL_DIR"] == "already-set"
        config.load_dotenv(env, override=True)
        assert os.environ["KICAD_SYMBOL_DIR"] == "C:\\libs\\symbols"
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_symbol_footprint_dir_first_existing(tmp_path, monkeypatch) -> None:
    real = tmp_path / "syms"
    real.mkdir()
    missing = tmp_path / "nope"

    monkeypatch.setenv("KICAD_SYMBOL_DIR", os.pathsep.join([str(missing), str(real)]))
    assert config.symbol_dir() == real
    # With nothing configured AND nothing discoverable, the answer is None. The
    # discovery fallback is stubbed out so the assertion does not depend on
    # whether the machine running the tests has KiCad installed.
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    monkeypatch.setattr(config, "_first_discovered", lambda kind: None)
    assert config.footprint_dir() is None


# --- library path fallback (env authoritative, discovery as backstop) ------ #


def test_env_wins_over_discovery(tmp_path, monkeypatch) -> None:
    """A configured path must never be overridden by a discovered install."""
    configured = tmp_path / "configured"
    configured.mkdir()
    discovered = tmp_path / "discovered"
    discovered.mkdir()
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(configured))
    monkeypatch.setattr(config, "_first_discovered", lambda kind: discovered)
    assert config.symbol_dir() == configured


def test_discovery_used_when_env_unset(tmp_path, monkeypatch) -> None:
    """The regression this fixes: libraries present but unconfigured."""
    discovered = tmp_path / "kicad-symbols"
    discovered.mkdir()
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.setattr(config, "_first_discovered", lambda kind: discovered)
    assert config.symbol_dir() == discovered


def test_discovery_used_when_env_path_absent(tmp_path, monkeypatch) -> None:
    """A stale env value is no better than no value, so discovery still runs."""
    discovered = tmp_path / "kicad-footprints"
    discovered.mkdir()
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(tmp_path / "deleted-long-ago"))
    monkeypatch.setattr(config, "_first_discovered", lambda kind: discovered)
    assert config.footprint_dir() == discovered


def test_discovery_reads_the_matching_kind(monkeypatch) -> None:
    """Symbols must not be answered with the footprint directory."""
    seen: list[str] = []

    def record(kind: str):
        seen.append(kind)
        return None

    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    monkeypatch.setattr(config, "_first_discovered", record)
    config.symbol_dir()
    config.footprint_dir()
    assert seen == ["symbols", "footprints"]


def test_first_discovered_skips_nonexistent(tmp_path, monkeypatch) -> None:
    """Discovery returns the first directory that actually exists."""
    from ratsnestpro.eda.vendor import kicad_paths

    absent = tmp_path / "absent"
    present = tmp_path / "present"
    present.mkdir()
    monkeypatch.setattr(kicad_paths, "symbol_dirs", lambda: [absent, present])
    assert config._first_discovered("symbols") == present


# --- footprint pads (fixture .pretty, offline) ---------------------------- #


def _mod(pads: str) -> str:
    return f'(footprint "TEST" (layer "F.Cu") {pads})'


def test_footprint_pads_from_fixture(tmp_path, monkeypatch) -> None:
    pretty = tmp_path / "MyLib.pretty"
    pretty.mkdir()
    mod = _mod(
        '(pad "1" smd rect (at -1.0 0.5) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))'
        '(pad "2" smd rect (at 1.0 -0.5) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))'
    )
    (pretty / "Part.kicad_mod").write_text(mod, encoding="utf-8")
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(tmp_path))

    pads = footprints.footprint_pads("MyLib:Part")
    assert pads is not None
    assert {p["number"] for p in pads} == {"1", "2"}
    bbox = footprints.footprint_bbox("MyLib:Part")
    assert bbox == (-1.0, -0.5, 1.0, 0.5)


def test_footprint_pads_unknown_returns_none(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(tmp_path))
    assert footprints.footprint_pads("Nope:Nope") is None
    assert footprints.footprint_bbox("Nope:Nope") is None


def test_footprint_courtyard_bbox_preferred(tmp_path, monkeypatch) -> None:
    pretty = tmp_path / "MyLib.pretty"
    pretty.mkdir()
    (pretty / "Part.kicad_mod").write_text(
        """
        (footprint "Part"
          (fp_rect (start -3 -2) (end 3 2)
            (stroke (width 0.05) (type solid)) (fill none)
            (layer "F.CrtYd"))
          (pad "1" smd rect (at -1 0) (size 1 1)
            (layers "F.Cu" "F.Paste" "F.Mask"))
          (pad "2" smd rect (at 1 0) (size 1 1)
            (layers "F.Cu" "F.Paste" "F.Mask")))
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(tmp_path))
    assert footprints.footprint_courtyard_bbox("MyLib:Part") == (
        -3.0,
        -2.0,
        3.0,
        2.0,
    )
