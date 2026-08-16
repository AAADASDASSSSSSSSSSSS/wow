"""Preflight environment probes: self-heal first, never report a silent pass.

Two properties are load-bearing and each has a test here:

* a probe distinguishes ``env`` from ``discovered`` from ``missing``, because
  "configured" and "found anyway" are both successes but only one of them is
  reproducible on another machine;
* a missing dependency produces a WARNING that says what stopped being
  verified, and never blocks the step. An unavailable check is not a failed
  check, and it is certainly not a passed one.
"""

from __future__ import annotations

import pytest

from ratsnestpro import config
from ratsnestpro.domain.contracts import RequirementSpec, Severity
from ratsnestpro.eda import preflight as pf
from ratsnestpro.orchestration.pipeline import (
    PipelineContext,
    PipelineState,
    RequirementsStep,
    _preflight_checks,
)


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Keep a developer's real .env out of the assertions."""
    monkeypatch.setattr(config, "_ENV_LOADED", True)
    yield


def _probe(report: pf.Preflight, name: str) -> pf.Probe:
    return report.get(name)


# --- directory probes ----------------------------------------------------- #


def test_configured_directory_reports_env(tmp_path, monkeypatch) -> None:
    libs = tmp_path / "symbols"
    libs.mkdir()
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(libs))
    probe = _probe(pf.preflight(), pf.SYMBOL_LIBRARY)
    assert probe.source == "env"
    assert probe.resolved_path == str(libs)
    assert probe.available


def test_discovered_directory_reports_discovered(tmp_path, monkeypatch) -> None:
    found = tmp_path / "kicad-symbols"
    found.mkdir()
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.setattr(config, "_first_discovered", lambda kind: found)
    probe = _probe(pf.preflight(), pf.SYMBOL_LIBRARY)
    assert probe.source == "discovered"
    assert probe.resolved_path == str(found)
    assert probe.available


def test_absent_directory_reports_missing(monkeypatch) -> None:
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    monkeypatch.setattr(config, "_first_discovered", lambda kind: None)
    report = pf.preflight()
    for name in (pf.SYMBOL_LIBRARY, pf.FOOTPRINT_LIBRARY):
        probe = _probe(report, name)
        assert probe.source == "missing"
        assert not probe.available
        assert name in report.missing


def test_stale_env_path_falls_through_to_discovery(tmp_path, monkeypatch) -> None:
    """An env var naming a deleted directory must not win over a real install."""
    found = tmp_path / "real"
    found.mkdir()
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(tmp_path / "gone"))
    monkeypatch.setattr(config, "_first_discovered", lambda kind: found)
    probe = _probe(pf.preflight(), pf.FOOTPRINT_LIBRARY)
    assert probe.source == "discovered"
    assert probe.resolved_path == str(found)


# --- tool probes ---------------------------------------------------------- #


def test_missing_kicad_cli_reports_missing(monkeypatch) -> None:
    from ratsnestpro.eda.vendor import kicad_cli

    def boom(explicit=None):
        raise kicad_cli.KicadCliNotFound("stubbed")

    monkeypatch.setattr(kicad_cli, "find_kicad_cli", boom)
    probe = _probe(pf.preflight(), pf.KICAD_CLI)
    assert probe.source == "missing"
    assert "ERC/DRC" in probe.verifies


def test_freerouting_env_override_reports_env(tmp_path, monkeypatch) -> None:
    exe = tmp_path / "freerouting.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("FREEROUTING_EXE", str(exe))
    probe = _probe(pf.preflight(), pf.FREEROUTING)
    assert probe.source == "env"
    assert probe.resolved_path == str(exe)


def test_missing_freerouting_reports_missing(monkeypatch) -> None:
    from ratsnestpro.eda import routing

    monkeypatch.delenv("FREEROUTING_EXE", raising=False)
    monkeypatch.setattr(routing, "freerouting_exe", lambda: None)
    probe = _probe(pf.preflight(), pf.FREEROUTING)
    assert probe.source == "missing"


def test_jlcpcb_db_missing_when_file_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KICAD_MCP_HOME", str(tmp_path))
    probe = _probe(pf.preflight(), pf.JLCPCB_DB)
    assert probe.source == "missing"


def test_jlcpcb_db_env_when_file_present(tmp_path, monkeypatch) -> None:
    (tmp_path / "jlcpcb.sqlite").write_text("", encoding="utf-8")
    monkeypatch.setenv("KICAD_MCP_HOME", str(tmp_path))
    probe = _probe(pf.preflight(), pf.JLCPCB_DB)
    assert probe.source == "env"


# --- messages never claim success ----------------------------------------- #


def test_missing_message_names_what_is_unverified() -> None:
    probe = pf.Probe(
        "symbol_library", "missing", env_var="KICAD_SYMBOL_DIR", verifies="symbol pin numbers"
    )
    message = probe.message()
    assert "not found" in message
    assert "symbol pin numbers not verified" in message
    assert "KICAD_SYMBOL_DIR" in message


def test_resolved_message_states_the_source(tmp_path) -> None:
    probe = pf.Probe("symbol_library", "discovered", resolved_path=str(tmp_path))
    message = probe.message()
    assert "discovered" in message
    assert str(tmp_path) in message
    # "ok" would erase the difference between configured and merely found.
    assert "ok" not in message.lower()


# --- integration with the requirements step ------------------------------- #


def test_preflight_checks_are_warnings_only(monkeypatch) -> None:
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    monkeypatch.setattr(config, "_first_discovered", lambda kind: None)
    checks = _preflight_checks()
    assert checks, "preflight must contribute at least one check"
    assert all(c.severity == Severity.WARNING for c in checks)
    assert all(c.name.startswith("tool_unavailable.") for c in checks)


def test_missing_environment_does_not_block_the_step(monkeypatch) -> None:
    """The decision: probe, report, proceed. A missing tool is not a design defect."""
    monkeypatch.delenv("KICAD_SYMBOL_DIR", raising=False)
    monkeypatch.delenv("KICAD_FOOTPRINT_DIR", raising=False)
    monkeypatch.setattr(config, "_first_discovered", lambda kind: None)

    step = RequirementsStep()
    state = PipelineState(requirement_text="an STM32F103 board", project_name="p")
    result = step.run(state, PipelineContext())

    assert not result.blocked
    unavailable = [c for c in result.checks if c.name.startswith("tool_unavailable.")]
    assert unavailable, "the step must surface the environment it could not verify"
    assert any(not c.ok for c in unavailable)


def test_available_environment_reports_ok_checks(tmp_path, monkeypatch) -> None:
    libs = tmp_path / "libs"
    libs.mkdir()
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(libs))
    monkeypatch.setenv("KICAD_FOOTPRINT_DIR", str(libs))
    checks = {c.name: c for c in _preflight_checks()}
    assert checks["tool_unavailable.symbol_library"].ok
    assert checks["tool_unavailable.footprint_library"].ok


def test_requirements_check_keeps_its_own_bottom_line() -> None:
    """Adding probes must not displace the step's own check.

    ``raw_text`` is already ``min_length=1`` on the contract, so an empty
    requirement cannot reach the check at all — construction fails first. What
    is worth pinning is that ``requirement_text_present`` still runs and is
    still ERROR-severity next to the new WARNING-only probes.
    """
    step = RequirementsStep()
    state = PipelineState(requirement_text="an STM32F103 board", project_name="p")
    checks = step.check(state, RequirementSpec(raw_text="an STM32F103 board", project_name="p"))
    text_check = next(c for c in checks if c.name == "requirement_text_present")
    assert text_check.ok
    assert text_check.severity == Severity.ERROR
    assert any(c.name.startswith("tool_unavailable.") for c in checks)


def test_empty_requirement_is_rejected_by_the_contract() -> None:
    """Documents where the empty-text bottom line actually lives."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        RequirementSpec(raw_text="", project_name="p")
