"""Resolve KiCAD footprints to real pad geometry.

A thin, typed adapter over the vendored ``footprint`` reader. Footprint
libraries use the standard ``<nick>.pretty/<name>.kicad_mod`` layout, which the
vendored resolver already understands via the ``KICAD_FOOTPRINT_DIR`` env var
(set by :mod:`ratsnestpro.config`). This module normalizes the vendored output
into flat ``{number, x, y, layers}`` pad dicts and exposes a footprint bounding
box for courtyard / placement checks later in the pipeline.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from ratsnestpro.eda.vendor.footprint import (
    load_footprint_node,
    pad_offsets,
    resolve_footprint,
)
from ratsnestpro.eda.vendor.sexpr import find_all, find_first

__all__ = [
    "resolve_footprint",
    "footprint_pads",
    "footprint_bbox",
    "footprint_courtyard_bbox",
]


def footprint_pads(lib_id: str) -> list[dict[str, Any]] | None:
    """Return pads of ``Lib:Name`` as ``{number, x, y, layers}``, or ``None``.

    Positions are relative to the footprint origin (mm).
    """
    path = resolve_footprint(lib_id)
    if not path:
        return None
    node = load_footprint_node(path)
    out: list[dict[str, Any]] = []
    for pad in pad_offsets(node):
        rel = pad.get("rel", (0.0, 0.0))
        out.append(
            {
                "number": pad.get("number", ""),
                "x": float(rel[0]),
                "y": float(rel[1]),
                "layers": pad.get("layers", []),
            }
        )
    return out or None


def footprint_bbox(lib_id: str) -> tuple[float, float, float, float] | None:
    """Axis-aligned bounding box (x1, y1, x2, y2) of a footprint's pads (mm).

    A coarse extent derived from pad centers; refined courtyard handling comes
    in the placement tasks. Returns ``None`` when the footprint is unresolved
    or has no pads.
    """
    pads = footprint_pads(lib_id)
    if not pads:
        return None
    xs = [p["x"] for p in pads]
    ys = [p["y"] for p in pads]
    return (min(xs), min(ys), max(xs), max(ys))


def _xy(node: list | None) -> tuple[float, float] | None:
    if node is None or len(node) < 3:
        return None
    return float(str(node[1])), float(str(node[2]))


@lru_cache(maxsize=4096)
def _courtyard_bbox_from_path(
    path_text: str,
    modified_ns: int,
) -> tuple[float, float, float, float] | None:
    del modified_ns  # part of the cache key so an edited library is re-read
    node = load_footprint_node(path_text)
    points: list[tuple[float, float]] = []
    for tag in ("fp_line", "fp_rect", "fp_arc", "fp_poly", "fp_circle"):
        for graphic in find_all(node, tag):
            layer = find_first(graphic, "layer")
            if layer is None or len(layer) < 2:
                continue
            if str(layer[1]) not in {"F.CrtYd", "B.CrtYd"}:
                continue
            local: list[tuple[float, float]] = []
            for point_tag in ("start", "mid", "end", "center"):
                point = _xy(find_first(graphic, point_tag))
                if point is not None:
                    local.append(point)
            pts = find_first(graphic, "pts")
            if pts is not None:
                local.extend(
                    point
                    for child in find_all(pts, "xy")
                    if (point := _xy(child)) is not None
                )
            if tag == "fp_circle":
                center = _xy(find_first(graphic, "center"))
                edge = _xy(find_first(graphic, "end"))
                if center is not None and edge is not None:
                    radius = math.dist(center, edge)
                    local.extend([
                        (center[0] - radius, center[1] - radius),
                        (center[0] + radius, center[1] + radius),
                    ])
            points.extend(local)
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def footprint_courtyard_bbox(
    lib_id: str,
) -> tuple[float, float, float, float] | None:
    """Return the real F/B.CrtYd extent, falling back to pad-center geometry."""
    path = resolve_footprint(lib_id)
    if path is None:
        return None
    courtyard = _courtyard_bbox_from_path(str(path), path.stat().st_mtime_ns)
    return courtyard if courtyard is not None else footprint_bbox(lib_id)


def footprint_path(lib_id: str) -> Path | None:
    """Resolve ``Lib:Name`` to its ``.kicad_mod`` path, or ``None``."""
    return resolve_footprint(lib_id)


def _demo(argv: list[str]) -> int:  # pragma: no cover - CLI convenience
    from ratsnestpro import config

    if not argv:
        cap = config.process_capability()
        print(f"process: {cap.fab_house} / {cap.profile}")
        print(f"  min_track_width = {cap.min_track_width} mm")
        print(f"  min_clearance   = {cap.min_clearance} mm")
        print(f"  min_via_diameter= {cap.min_via_diameter} mm")
        print("usage: python -m ratsnestpro.eda.footprints <Lib:Name> [...]")
        return 0
    rc = 0
    for lib_id in argv:
        pads = footprint_pads(lib_id)
        if pads is None:
            print(f"{lib_id}: NOT FOUND")
            rc = 1
            continue
        print(f"{lib_id}  ({len(pads)} pads)  <- {footprint_path(lib_id)}")
        for p in pads[:8]:
            print(f"  pad {p['number']:>4} @ ({p['x']:.3f}, {p['y']:.3f})  {p['layers']}")
        if len(pads) > 8:
            print(f"  ... (+{len(pads) - 8} more)")
    return rc


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_demo(sys.argv[1:]))
