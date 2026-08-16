"""KiCad's own netlist, used as the truth source for pin -> net.

Why shell out instead of computing connectivity ourselves
---------------------------------------------------------
``vendor.connectivity.SchematicGraph`` unions wire *endpoints*. A pin that lands
on the middle of a wire segment is therefore not joined to it, and the module
says as much about itself: pin attachment is best-effort. Measured against this
exporter on the shipped ``pic_programmer`` demo, that path recovered 48 of 236
pin -> net entries and agreed on 25 of those 48 net names, which produced 34
false shorts on the demo corpus. Net membership is not a place where
best-effort is useful: every connection check is a statement about it.

``kicad-cli sch export netlist`` is the same netlister Eeschema uses, so its
answer is the one KiCad itself would act on. It also resolves the whole
hierarchy in a single call, which matters more than it first appears --
``SchematicDoc.load()`` parses one sheet, so a hierarchical project read that
way silently omits every component on a child sheet.

What this module reads from the export, and why all of it
---------------------------------------------------------
All three of ``components`` / ``libparts`` / ``nets`` come from the same export
so that the reference sets cannot disagree. Taking components from a separate
parse of the root sheet was the original plan; it would leave ``pin_nets``
entries pointing at components that ``parts`` does not contain, because the
netlist covers child sheets and a single-sheet parse does not.

``no_connect`` is filled here, unlike in a file-only reading. KiCad records a
no-connect as a coordinate marker, and attributing one to a pin means matching
positions -- a wrong attribution would silently suppress a real dangling-pin
finding. The exporter does that attribution itself and reports the outcome as a
``+no_connect`` suffix on the node's ``pintype``, so the attribution is KiCad's
rather than a guess. Verified on ``pic_programmer``: 77 ``no_connect`` elements
across its two sheets, 77 tagged nodes, and the two sets are identical.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ratsnestpro.eda.vendor.sexpr import Node, find_all, find_first, loads, tag_of

__all__ = [
    "KicadNetlist",
    "NetlistComponent",
    "NetlistError",
    "clear_cache",
    "export_netlist",
    "netlist_for_schematic",
    "parse_netlist",
]

# The suffix KiCad appends to a node's ``pintype`` when the pin carries a
# no-connect marker, e.g. ``passive+no_connect``.
_NO_CONNECT_SUFFIX = "no_connect"


class NetlistError(RuntimeError):
    """Raised when a netlist cannot be produced or the output is unusable."""


@dataclass(frozen=True)
class NetlistComponent:
    """One annotated component instance, as the netlister resolved it."""

    ref: str
    lib_id: str
    value: str
    footprint: str


@dataclass
class KicadNetlist:
    """Pin-level connectivity of a whole schematic hierarchy.

    ``pins`` is keyed by reference and holds ``number`` / ``name`` / ``type``,
    matching what :func:`ratsnestpro.eda.adapter.SchematicDoc.pin_geometry`
    returns for the keys any check actually reads. The pins come from the
    ``libparts`` section, which is a symbol *definition* pin list. That is the
    right granularity here: a reference designates a whole component, so a
    multi-unit symbol's units all belong to it, whereas an instance-level read
    would see only the unit that happens to be placed.
    """

    components: dict[str, NetlistComponent]
    pins: dict[str, list[dict[str, object]]]
    pin_nets: dict[tuple[str, str], str]
    no_connect: set[tuple[str, str]]

    @property
    def net_names(self) -> set[str]:
        return set(self.pin_nets.values())


def _first_value(node: list, tag: str) -> str:
    """The single value of ``(tag "value")``, or ``""`` when absent or empty.

    ``(name)`` with no value is how the exporter writes an empty field, so a
    missing payload is expected rather than exceptional.
    """
    child = find_first(node, tag)
    if child is None or len(child) < 2:
        return ""
    return str(child[1]).strip()


def _as_list(node: Node | None) -> list:
    return node if isinstance(node, list) else []


def parse_netlist(text: str) -> KicadNetlist:
    """Parse a ``--format kicadsexpr`` netlist. Does not need KiCad installed.

    Kept separate from :func:`export_netlist` so the parsing rules can be tested
    against recorded output on a host with no KiCad.
    """
    root = loads(text)
    if not isinstance(root, list) or tag_of(root) != "export":
        raise NetlistError("not a KiCad netlist: top-level expression is not (export ...)")

    # libparts first: components reference them, so the pin lists must exist
    # before a component can be given its pins.
    pins_by_libpart: dict[tuple[str, str], list[dict[str, object]]] = {}
    for libpart in find_all(_as_list(find_first(root, "libparts")), "libpart"):
        key = (_first_value(libpart, "lib"), _first_value(libpart, "part"))
        entries: list[dict[str, object]] = []
        for pin in find_all(_as_list(find_first(libpart, "pins")), "pin"):
            number = _first_value(pin, "num")
            if not number:
                continue
            entries.append(
                {
                    "number": number,
                    "name": _first_value(pin, "name"),
                    "type": _first_value(pin, "type"),
                }
            )
        pins_by_libpart[key] = entries

    components: dict[str, NetlistComponent] = {}
    pins: dict[str, list[dict[str, object]]] = {}
    for comp in find_all(_as_list(find_first(root, "components")), "comp"):
        ref = _first_value(comp, "ref")
        if not ref:
            continue
        libsource = find_first(comp, "libsource")
        lib = _first_value(libsource, "lib") if libsource is not None else ""
        part = _first_value(libsource, "part") if libsource is not None else ""
        components[ref] = NetlistComponent(
            ref=ref,
            # Recomposed rather than read: the netlist splits what the schematic
            # stores as one ``lib_id``, and every consumer here expects the
            # ``lib:part`` form.
            lib_id=f"{lib}:{part}" if lib and part else part,
            value=_first_value(comp, "value"),
            footprint=_first_value(comp, "footprint"),
        )
        pins[ref] = list(pins_by_libpart.get((lib, part), []))

    pin_nets: dict[tuple[str, str], str] = {}
    no_connect: set[tuple[str, str]] = set()
    for net in find_all(_as_list(find_first(root, "nets")), "net"):
        # Kept verbatim, including the leading '/' a hierarchical local label
        # carries. This is the truth source; renaming its nets would make it
        # something else.
        name = _first_value(net, "name")
        if not name:
            continue
        for node in find_all(net, "node"):
            ref = _first_value(node, "ref")
            number = _first_value(node, "pin")
            if not ref or not number:
                continue
            pin_nets[(ref, number)] = name
            if _NO_CONNECT_SUFFIX in _first_value(node, "pintype").split("+"):
                no_connect.add((ref, number))

    return KicadNetlist(
        components=components,
        pins=pins,
        pin_nets=pin_nets,
        no_connect=no_connect,
    )


def export_netlist(sch_path: str | Path, *, cli_path: str | None = None) -> str:
    """Run ``kicad-cli sch export netlist`` and return the netlist text.

    Raises :class:`NetlistError` rather than returning something empty, because
    an empty netlist and a failed export are indistinguishable downstream and
    the second one must not be read as "this board has no connections".
    """
    from ratsnestpro.eda.vendor.kicad_cli import KicadCli, KicadCliNotFound

    source = Path(sch_path)
    if not source.is_file():
        raise NetlistError(f"schematic not found: {source}")
    try:
        cli = KicadCli(cli_path)
    except KicadCliNotFound as exc:
        raise NetlistError(str(exc)) from exc

    with tempfile.TemporaryDirectory(prefix="rnp-netlist-") as tmp:
        out = Path(tmp) / "netlist.net"
        result = cli.export_netlist(str(source), str(out))
        if not result.ok or not out.is_file():
            detail = (result.stderr or result.stdout or "").strip()
            raise NetlistError(
                f"kicad-cli sch export netlist failed for {source.name} "
                f"(exit {result.returncode}): {detail or 'no output'}"
            )
        return out.read_text(encoding="utf-8")


@lru_cache(maxsize=64)
def _cached(path: str, stamp: tuple[int, int], cli_path: str | None) -> KicadNetlist:
    return parse_netlist(export_netlist(path, cli_path=cli_path))


def clear_cache() -> None:
    """Drop memoised netlists.

    Needed by anything that changes what an export would return without changing
    the root sheet's mtime -- a stubbed-out CLI, or an edit to a child sheet.
    """
    _cached.cache_clear()


def netlist_for_schematic(
    sch_path: str | Path,
    *,
    cli_path: str | None = None,
) -> KicadNetlist:
    """Export and parse in one step, memoised on the root sheet's mtime and size.

    The cache key covers the root sheet only. A hierarchical project's child
    sheets can therefore change without invalidating it -- acceptable for
    read-only corpus analysis, where the files do not move under us, and worth
    stating because it would not be acceptable for a sheet the pipeline writes.
    """
    resolved = Path(sch_path)
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise NetlistError(f"schematic not readable: {resolved} ({exc})") from exc
    return _cached(str(resolved), (stat.st_mtime_ns, stat.st_size), cli_path)
