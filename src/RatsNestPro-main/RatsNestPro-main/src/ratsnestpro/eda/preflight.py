"""Environment probe: what the deterministic checks can actually verify.

Every bottom-line check in the pipeline reads a real library, a real table or a
real tool. When one of those is absent the check does not become false — it
becomes *unanswerable*, and the two outcomes must never be reported the same
way. :mod:`ratsnestpro.orchestration.pipeline` already models this correctly for
``kicad_cli_erc`` ("unavailable is a warning, never a pass"); this module makes
the same discipline available to every other environment dependency, in one
place, at the first step of the run.

Self-healing before reporting
-----------------------------
Most "missing" environments are not missing at all, only unconfigured. KiCad
ships its symbol and footprint libraries next to the binary, and
:mod:`ratsnestpro.eda.vendor.kicad_paths` already discovers system, per-user and
POSIX install layouts. So each probe resolves in three tiers and reports which
one answered:

``env``
    An environment variable named an existing path. Authoritative.
``discovered``
    Nothing was configured, but the dependency was located automatically. This
    is a *successful* outcome — the run proceeds with real grounding.
``missing``
    Neither worked. The run still proceeds (a probe never blocks), but the
    checks that depend on it must say "not verified" rather than "passed".

No environment variable is invented here: a probe reports ``env`` only for
variables the rest of the codebase already honours.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ratsnestpro import config

__all__ = [
    "Source",
    "Probe",
    "Preflight",
    "preflight",
    "SYMBOL_LIBRARY",
    "FOOTPRINT_LIBRARY",
    "KICAD_CLI",
    "KICAD_PYTHON",
    "FREEROUTING",
    "JLCPCB_DB",
]

Source = Literal["env", "discovered", "missing"]

SYMBOL_LIBRARY = "symbol_library"
FOOTPRINT_LIBRARY = "footprint_library"
KICAD_CLI = "kicad_cli"
KICAD_PYTHON = "kicad_python"
FREEROUTING = "freerouting"
JLCPCB_DB = "jlcpcb_db"


@dataclass(frozen=True)
class Probe:
    """One environment dependency and how it was resolved."""

    name: str
    source: Source
    resolved_path: str = ""
    env_var: str = ""
    verifies: str = ""

    @property
    def available(self) -> bool:
        return self.source != "missing"

    def message(self) -> str:
        """Human-readable outcome.

        Deliberately never says "ok": a present dependency reports *where* it
        came from, and an absent one reports what stopped being verified.
        """
        if self.source == "missing":
            hint = f" set {self.env_var}" if self.env_var else ""
            suffix = f"; {self.verifies} not verified" if self.verifies else ""
            return f"{self.name} not found{suffix}.{hint}".rstrip()
        return f"{self.name} resolved from {self.source}: {self.resolved_path}"


@dataclass(frozen=True)
class Preflight:
    """Result of probing every environment dependency."""

    probes: tuple[Probe, ...]

    def get(self, name: str) -> Probe:
        for probe in self.probes:
            if probe.name == name:
                return probe
        raise KeyError(name)

    def available(self, name: str) -> bool:
        return self.get(name).available

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.probes if not p.available)

    def summary(self) -> str:
        if not self.missing:
            return f"environment complete ({len(self.probes)} dependencies resolved)"
        return f"{len(self.missing)} of {len(self.probes)} unavailable: {', '.join(self.missing)}"


def _dir_probe(name: str, env_var: str, kind: str, verifies: str) -> Probe:
    """Probe a library directory, distinguishing configured from discovered."""
    configured = os.environ.get(env_var)
    if configured:
        for part in configured.split(os.pathsep):
            if part and Path(part).exists():
                return Probe(name, "env", part, env_var, verifies)
    # Not configured, or configured at a path that does not exist. Either way
    # discovery is the remaining chance, and config.* resolves it the same way.
    resolved = config.symbol_dir() if kind == "symbols" else config.footprint_dir()
    if resolved is not None:
        return Probe(name, "discovered", str(resolved), env_var, verifies)
    return Probe(name, "missing", "", env_var, verifies)


def _kicad_cli_probe() -> Probe:
    from ratsnestpro.eda.vendor.kicad_cli import KicadCliNotFound, find_kicad_cli

    verifies = "kicad-cli ERC/DRC and Gerber export"
    try:
        found = find_kicad_cli()
    except KicadCliNotFound:
        return Probe(KICAD_CLI, "missing", "", "", verifies)
    return Probe(KICAD_CLI, "discovered", found, "", verifies)


def _kicad_python_probe() -> Probe:
    from ratsnestpro.eda import routing

    found = routing.kicad_python()
    verifies = "DSN export and SES import"
    if not found:
        return Probe(KICAD_PYTHON, "missing", "", "", verifies)
    return Probe(KICAD_PYTHON, "discovered", found, "", verifies)


def _freerouting_probe() -> Probe:
    from ratsnestpro.eda import routing

    verifies = "signal routing completeness"
    override = os.environ.get("FREEROUTING_EXE")
    if override and Path(override).is_file():
        return Probe(FREEROUTING, "env", override, "FREEROUTING_EXE", verifies)
    found = routing.freerouting_exe()
    if not found:
        return Probe(FREEROUTING, "missing", "", "FREEROUTING_EXE", verifies)
    return Probe(FREEROUTING, "discovered", found, "FREEROUTING_EXE", verifies)


def _jlcpcb_probe() -> Probe:
    from ratsnestpro.eda.vendor.jlcpcb import db_path

    verifies = "grounded MPN/LCSC part data"
    path = db_path()
    if not path.is_file():
        return Probe(JLCPCB_DB, "missing", "", "KICAD_MCP_HOME", verifies)
    source: Source = "env" if os.environ.get("KICAD_MCP_HOME") else "discovered"
    return Probe(JLCPCB_DB, source, str(path), "KICAD_MCP_HOME", verifies)


def preflight() -> Preflight:
    """Probe every environment dependency the deterministic checks rely on.

    Intentionally uncached: the probes are a handful of ``Path.exists`` calls and
    one ``PATH`` scan, while caching would freeze a stale answer across an
    environment change and reintroduce exactly the kind of divergence this
    module exists to remove.
    """
    config.init_env()
    return Preflight(
        (
            _dir_probe(
                SYMBOL_LIBRARY, "KICAD_SYMBOL_DIR", "symbols", "symbol pin numbers and graphics"
            ),
            _dir_probe(
                FOOTPRINT_LIBRARY, "KICAD_FOOTPRINT_DIR", "footprints", "footprint pad geometry"
            ),
            _kicad_cli_probe(),
            _kicad_python_probe(),
            _freerouting_probe(),
            _jlcpcb_probe(),
        )
    )
