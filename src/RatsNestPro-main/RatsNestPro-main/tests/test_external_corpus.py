"""The external fixture corpus, used according to what it can actually evidence.

Three layers, strongest first:

1. ``ground_pin_test_*`` — five sheets that differ only in how ground is wired,
   four asserted defective by KiCad's own ``test_erc_ground_pins.cpp`` and one
   named ``_ok``. Paired samples, so both firing and staying silent are checked.
2. ``role=negative`` root sheets — the false-positive baseline. Upstream expects
   these clean, so any ERROR here is ours to explain.
3. A recorded measurement that this corpus cannot evidence fact-sheet checks, so
   a later reader does not mistake a clean sweep for coverage.

``role=excluded`` (242 of the 337 schematics) is not used at all: upstream makes
no claim about those files, and a baseline built on them would mean nothing.

Everything here needs ``RATSNESTPRO_FIXTURE_HOME`` and a real ``kicad-cli``, and
skips without them.
"""

from __future__ import annotations

import pytest

from ratsnestpro.eda import factgate
from ratsnestpro.orchestration import pipeline as P
from tests.fixtures import kicad_demos as demos
from tests.fixtures import kicad_fixtures as ext

pytestmark = [ext.requires_fixture_home, demos.requires_kicad_cli]

# Every check that reads only a connectivity view, so it can run against a file.
# Checks keyed on ``role`` are included deliberately: with ``role`` empty they
# must find no candidates and stay silent, and that silence is worth asserting.
_VIEW_CHECKS = (
    ("power_pin_rail_class", P._power_pin_rail_checks),
    ("critical_power_reset_pins_connected", P._critical_function_pin_checks),
    ("crystal_topology", P._crystal_topology_checks),
    ("led_series", P._led_series_checks),
    ("two_terminal_not_shorted", P._two_terminal_short_checks),
    ("mechanical_part", P._mechanical_part_checks),
    ("swd_topology", P._swd_topology_checks),
    ("can_topology", P._can_topology_checks),
    ("analog_input", P._analog_input_topology_checks),
    ("buck_topology", P._buck_topology_checks),
    ("power_mux", P._power_mux_topology_checks),
    ("supply_pin_not_on_regulator_input", P._mcu_supply_source_checks),
)


def _failures(view: P._ConnectivityView) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for label, run in _VIEW_CHECKS:
        for check in run(view):
            if not check.ok:
                out.append((label, check.message))
    return out


# --------------------------------------------------------------------------- #
# Layer 1: paired samples with an upstream assertion
# --------------------------------------------------------------------------- #


def test_ground_pin_corpus_is_complete() -> None:
    """Five sheets, four defective and one clean. A shrunken set proves less."""
    cases = ext.ground_pin_cases()
    assert len(cases) == 5, [c.path.name for c in cases]
    assert sum(1 for c in cases if c.role == "positive") == 4
    assert [c.path.name for c in cases if c.role == "negative"] == [
        "ground_pin_test_ok.kicad_sch"
    ]
    for case in cases:
        assert case.is_root_sheet, f"{case.path.name} has no .kicad_pro beside it"


def test_asserted_ground_defects_are_all_detected() -> None:
    """``power_pin_rail_class`` finds what KiCad asserts, from the file alone.

    Upstream states ``ERCE_GROUND_PIN_NOT_GROUND`` for these four. This check
    reaches the same verdict without reading KiCad's ERC output — the ground pin
    is on a net whose name does not denote ground — which is the only external
    evidence in this repository that a topology check detects a real defect
    rather than merely failing to misfire.
    """
    detected: dict[str, bool] = {}
    for case in ext.ground_pin_cases():
        if case.role != "positive":
            continue
        view = P._ConnectivityView.from_schematic(case.path)
        names = {
            check.name
            for check in P._power_pin_rail_checks(view)
            if not check.ok
        }
        detected[case.path.name] = "power_pin_rail_class" in names
    assert len(detected) == 4
    assert all(detected.values()), f"missed: {[k for k, v in detected.items() if not v]}"


def test_clean_companion_is_silent() -> None:
    """The ``_ok`` sheet is the same circuit wired correctly.

    Without this, a check that reported every sheet would pass the test above.
    """
    ok = next(c for c in ext.ground_pin_cases() if c.role == "negative")
    view = P._ConnectivityView.from_schematic(ok.path)
    assert view.ground_nets, "the clean sheet must resolve a ground net"
    assert not _failures(view)


# --------------------------------------------------------------------------- #
# Layer 2: false-positive baseline
# --------------------------------------------------------------------------- #


@pytest.mark.real_kicad
def test_negative_corpus_reports_nothing() -> None:
    """Upstream expects these clean, so every ERROR here is ours to explain.

    Two exclusions stand behind this being zero, both recorded with reasons in
    :mod:`tests.fixtures.kicad_fixtures`: KiBot's ``off-grid`` fixture is a single
    resistor rather than a circuit, and two upstream creepage sheets are ones
    ``kicad-cli`` itself refuses to load.

    ``BeagleBone-Black-Cape`` is in this set and is why ``_GROUND_NAME_TOKENS``
    grew: the official template names its digital ground ``GNDD``, which whole-
    token matching against ``GND`` did not recognise, so every ground pin on it
    was reported as not reaching ground.
    """
    findings: list[str] = []
    swept = 0
    for fixture in ext.fixtures(role="negative", kind="schematic", roots_only=True):
        try:
            view = P._ConnectivityView.from_schematic(fixture.path)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            findings.append(f"{fixture.relative}: export failed: {exc}")
            continue
        swept += 1
        findings.extend(
            f"{fixture.relative}: {label}: {message}"
            for label, message in _failures(view)
        )
    assert swept >= 30, f"only {swept} negative root sheets swept"
    assert not findings, "false positives on sheets upstream expects clean:\n  " + (
        "\n  ".join(findings)
    )


@pytest.mark.real_kicad
def test_negative_corpus_is_substantial_enough_to_mean_something() -> None:
    """A baseline over empty views would pass by resolving nothing."""
    parts = pins = 0
    for fixture in ext.fixtures(role="negative", kind="schematic", roots_only=True):
        try:
            view = P._ConnectivityView.from_schematic(fixture.path)
        except Exception:  # noqa: BLE001 - counted by the test above
            continue
        parts += len(view.parts)
        pins += len(view.pin_nets)
    assert parts >= 400, f"only {parts} components across the negative corpus"
    assert pins >= 1500, f"only {pins} pin->net entries"


# --------------------------------------------------------------------------- #
# Layer 3: what this corpus cannot show
# --------------------------------------------------------------------------- #


@pytest.mark.real_kicad
def test_corpus_cannot_evidence_fact_sheet_checks() -> None:
    """Recorded so a clean sweep is never mistaken for coverage.

    Measured 2026-08-03: 7 of 1988 components resolve to one of the 17 fact
    sheets, and no project directory contains a regulator with a sheet. Every
    gate needing two sheets — ``supply_pin_conflicts`` among them — is therefore
    structurally unreachable here.

    If this test starts failing because regulators appeared, that is good news:
    the corpus gained real designs, and the fact-sheet gates can then be
    evidenced against it.
    """
    with_regulator = 0
    for fixture in ext.fixtures(kind="schematic", roots_only=True):
        try:
            view = P._ConnectivityView.from_schematic(fixture.path)
        except Exception:  # noqa: BLE001 - unparsable files are layer 2's problem
            continue
        classes = {
            str(sheet.device_class)
            for _ref, sheet in factgate.resolve_sheets(list(view.parts.values()))
        }
        if {"ldo", "dcdc"} & classes:
            with_regulator += 1
    assert with_regulator == 0, (
        f"{with_regulator} projects now have a regulator with a fact sheet — the "
        "corpus can evidence cross-device supply gates; drop this expectation and "
        "add them to the baseline instead"
    )
