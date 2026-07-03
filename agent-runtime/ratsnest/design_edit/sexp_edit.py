"""Formatting-preserving property edits on .kicad_sch files.

Reuses kicad-happy's battle-tested `edit_properties.apply_updates` (bom skill)
via dynamic import — kicad-happy stays unforked, we consume its module as a
library. "Value" is itself a property in KiCad 6+ format, so set_value is a
property update too.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from ratsnest.config import Config

_cached_module = None


def _load_edit_properties(config: Config):
    global _cached_module
    if _cached_module is not None:
        return _cached_module
    scripts_dir = config.bom_scripts
    # kicad_sexp must be importable by edit_properties
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(
        "kh_edit_properties", scripts_dir / "edit_properties.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    _cached_module = module
    return module


def apply_property_updates(
    text: str,
    updates: dict[str, dict[str, str]],
    config: Config | None = None,
) -> tuple[str, list[dict]]:
    """Apply {reference: {property: value}} updates to schematic text.

    Returns (new_text, change_log). Entries with action=="error" mean the
    reference was not found — caller must treat the whole plan as failed.
    """
    module = _load_edit_properties(config or Config.load())
    return module.apply_updates(text, updates, dry_run=False)
