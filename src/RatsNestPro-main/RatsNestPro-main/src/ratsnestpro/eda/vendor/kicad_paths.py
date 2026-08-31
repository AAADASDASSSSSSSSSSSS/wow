"""Discover KiCAD installations across platforms and install styles.

Handles system installs (``C:\\Program Files\\KiCad\\<ver>``) and per-user
installs (``%LOCALAPPDATA%\\Programs\\KiCad\\<ver>``, used by KiCAD 10's
current-user installer), plus Linux/macOS locations. Version directories are
discovered dynamically, so new KiCAD releases are picked up without code
changes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List


def _install_roots() -> List[Path]:
    roots: List[Path] = []
    if os.name == "nt":
        bases = []
        for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            v = os.environ.get(env)
            if v:
                bases.append(Path(v) / "KiCad")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            bases.append(Path(local) / "Programs" / "KiCad")
        for base in bases:
            if base.exists():
                # Version sub-dirs (e.g. 10.0, 9.0); newest first.
                for ver in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
                    roots.append(ver)
    else:
        for base in ("/usr", "/usr/local", "/Applications/KiCad/KiCad.app/Contents"):
            p = Path(base)
            if p.exists():
                roots.append(p)
    return roots


def cli_candidates() -> List[Path]:
    out = []
    for root in _install_roots():
        if os.name == "nt":
            out.append(root / "bin" / "kicad-cli.exe")
        else:
            out.append(root / "bin" / "kicad-cli")
            out.append(root / "MacOS" / "kicad-cli")
    return out


def _share_dirs(kind: str) -> List[Path]:
    """kind is 'footprints' or 'symbols'."""
    out = []
    for root in _install_roots():
        # Linux and Windows packages use ``share/kicad``.  macOS application
        # bundles use ``Contents/SharedSupport`` (KiCad 10), while some older
        # or repackaged bundles put the same data under ``Resources``.  Keep
        # every layout here so config, grounding and preflight all discover the
        # exact same libraries without requiring machine-specific environment
        # variables.
        for sub in (
            root / "share" / "kicad" / kind,
            root / "SharedSupport" / kind,
            root / "SharedSupport" / "kicad" / kind,
            root / "Resources" / "share" / "kicad" / kind,
        ):
            if sub.exists():
                out.append(sub)
    # De-duplicate preserving order.
    seen = set()
    uniq = []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


def footprint_dirs() -> List[Path]:
    return _share_dirs("footprints")


def symbol_dirs() -> List[Path]:
    return _share_dirs("symbols")


def demo_dirs() -> List[Path]:
    """Directories holding the demo projects KiCAD ships with itself.

    Same discovery as :func:`symbol_dirs`, pointed at ``share/kicad/demos``.
    Useful as a read-only corpus of real, human-reviewed boards: they exercise
    parsers and checks without anything needing to be vendored into a repo.
    """
    return _share_dirs("demos")
