"""Ground LLM-proposed symbol/footprint names to real KiCad library IDs.

An LLM proposes plausible-looking library IDs (``Device:Resistor``,
``Resistor_SMD:R_0603``, ``MCU_ATmega:ATmega328P``) that frequently do not
match the *exact* names KiCad ships (``Device:R``, ``R_0603_1608Metric``,
``MCU_Microchip_ATmega:ATmega328P-A``). This module maps a proposal to a real,
existing library ID so the design can proceed — **without ever fabricating**:
if no real match is found the original string is returned unchanged, so the
bottom-line selection check still fails closed on a genuinely bad part.

Strategy (symbols): exact resolve → small EE name/lib alias (semantic maps that
lexical fuzzing cannot bridge, e.g. "Resistor"→"R") → fuzzy/substring match of
the name within the (aliased) library, then across all libraries.

Strategy (footprints): exact pad lookup → substring search (the vendored
search already substring-matches the module stem, so ``R_0603`` finds
``R_0603_1608Metric``) → closest match, preferring the proposed library.
"""

from __future__ import annotations

import difflib
import re
from functools import lru_cache
from pathlib import Path

from ratsnestpro.eda import footprints, symbols
from ratsnestpro.eda.vendor.library import footprint_roots, search_footprints
from ratsnestpro.eda.vendor.sexpr import find_all

__all__ = ["ground_symbol", "ground_footprint", "symbol_index"]

# Semantic name aliases: descriptive LLM name (lower) -> KiCad symbol name.
# These are *normalization* aids (not electrical rules); lexical fuzzing alone
# cannot get "Resistor" -> "R".
_NAME_ALIASES: dict[str, str] = {
    "resistor": "R",
    "res": "R",
    "capacitor": "C",
    "cap": "C",
    "capacitor_polarized": "C_Polarized",
    "capacitor_electrolytic": "C_Polarized",
    "inductor": "L",
    "ferrite_bead": "FerriteBead",
    "polyfuse": "Polyfuse_Small",
    "polyfuse_smd": "Polyfuse_Small",
    "fuse": "Fuse",
    "tvs": "D_TVS",
    "tvs_diode": "D_TVS",
    "diode": "D",
    "zener": "D_Zener",
    "schottky": "D_Schottky",
    "led": "LED",
    "switch": "SW_Push",
    "button": "SW_Push",
    "pushbutton": "SW_Push",
}
# Library-nick aliases: LLM lib (lower) -> real KiCad library nick.
_LIB_ALIASES: dict[str, str] = {
    "mcu_atmega": "MCU_Microchip_ATmega",
    "mcu_microchip": "MCU_Microchip_ATmega",
    "connector_usb": "Connector",
    "connector_pinheader_2.54mm": "Connector_Generic",
    "connector_pinheader": "Connector_Generic",
    "switch": "Switch",
}


def _tokens(name: str) -> set[str]:
    """Tokenize a library id/name for overlap scoring.

    Splits on non-alphanumeric AND on alpha/digit boundaries so ``C0603`` →
    ``{c, 0603}`` and ``R_0603_1608Metric`` → ``{r, 0603, 1608, metric}``.
    Connector counts are zero-pad normalized so ``2x03`` matches ``02x03``."""
    toks: set[str] = set()
    for raw in re.split(r"[^a-z0-9]+", name.lower()):
        if not raw:
            continue
        toks.add(raw)
        for piece in re.findall(r"[a-z]+|\d+", raw):  # split c0603 -> c, 0603
            toks.add(piece)
    for t in list(toks):  # NxM connector-count normalization
        m = re.fullmatch(r"(\d+)x(\d+)", t)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            toks.add(f"{a}x{b}")
            toks.add(f"{a:02d}x{b:02d}")
    return toks


def _best_by_tokens(requested: str, index: list[tuple[str, set[str]]]) -> str | None:
    """Pick the library id whose tokens best overlap ``requested``.

    Requires a *distinctive* overlap — either a token containing a digit
    (package size / pin count like ``0603``/``02x03``) or at least two shared
    tokens — so unrelated parts are not matched. Ties break toward the shortest
    id (the most generic variant)."""
    want = _tokens(requested)
    if not want:
        return None
    best_id: str | None = None
    best_score = 0.0
    for lib_id, toks in index:
        inter = want & toks
        if not inter:
            continue
        distinctive = any(any(ch.isdigit() for ch in t) for t in inter) or len(inter) >= 2
        if not distinctive:
            continue
        # Bonus when a requested token matches a WHOLE underscore-delimited
        # segment of the candidate — disambiguates e.g. imperial "0603"
        # (segment in ``C_0603_1608Metric``) from the metric 0603 that only
        # appears fused inside ``C_0201_0603Metric``.
        name = lib_id.partition(":")[2].lower()
        segs = {s for s in re.split(r"[^a-z0-9]+", name) if s}
        seg_bonus = len(want & segs)
        score = len(inter) + 2.0 * seg_bonus - 0.001 * len(lib_id)
        if score > best_score:
            best_score, best_id = score, lib_id
    return best_id


@lru_cache(maxsize=4)
def _footprint_index_for(roots: tuple[str, ...]) -> tuple[tuple[str, frozenset[str]], ...]:
    """All ``Lib:Footprint`` ids with pre-computed tokens for the given roots."""
    out: list[tuple[str, frozenset[str]]] = []
    for root_str in roots:
        for mod in Path(root_str).glob("*.pretty/*.kicad_mod"):
            lib_id = f"{mod.parent.stem}:{mod.stem}"
            out.append((lib_id, frozenset(_tokens(lib_id))))
    return tuple(out)


def _footprint_index() -> tuple[tuple[str, frozenset[str]], ...]:
    """All ``Lib:Footprint`` ids with tokens (cached per root set)."""
    return _footprint_index_for(tuple(str(r) for r in footprint_roots()))


# A top-level ``(symbol "NAME"`` sits at one tab of indentation in a
# ``.kicad_sym``; a unit inside it sits at two. Depth alone therefore separates
# the names this index wants from the unit sub-symbols it does not.
#
# The shortcut exists because the index needs nothing but those names, and
# obtaining them by parsing is what dominated a pipeline run: fully parsing the
# 222 stock KiCad 10 libraries costs about 110 s and 30 million parser calls,
# against 3.6 s for this regex. Verified identical on all 222.
_TOP_LEVEL_SYMBOL_RE = re.compile(r'^\t\(symbol "((?:[^"\\]|\\.)*)"', re.MULTILINE)


def _symbol_names_fast(path: Path) -> set[str] | None:
    """Top-level symbol names in a library, or None when the shortcut declines.

    Returning None rather than an empty set is the whole safety property. An
    empty index makes every grounding lookup silently fail and every part fall
    through unchanged, which looks exactly like "the library has no such symbol".
    So a file this does not recognise — different indentation, an escaped name,
    an unreadable file — must be handed to the real parser instead.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    names = _TOP_LEVEL_SYMBOL_RE.findall(text)
    if not names or any("\\" in name for name in names):
        return None
    return set(names)


@lru_cache(maxsize=4)
def _symbol_index_for(roots: tuple[str, ...]) -> tuple[str, ...]:
    """All available ``Lib:Name`` symbol IDs for the given roots (cached per
    root set so a changed ``KICAD_SYMBOL_DIR`` yields a fresh index)."""
    ids: set[str] = set()
    for root_str in roots:
        root = Path(root_str)
        # New directory layout — nick from the dir, name from the file stem.
        for f in root.glob("*.kicad_symdir/*.kicad_sym"):
            nick = f.parent.name[: -len(".kicad_symdir")]
            ids.add(f"{nick}:{f.stem}")
        # Legacy single-file layout — read the library for its symbol names.
        for f in root.glob("*.kicad_sym"):
            names = _symbol_names_fast(f)
            if names is None:
                # Building the ID index needs only symbol names. Retaining every
                # full KiCad library tree in the part-lookup cache costs multiple
                # gigabytes; parse each library transiently instead.
                node = symbols._read_lib_node(str(f))
                if node is None:
                    continue
                names = {
                    str(sym[1])
                    for sym in find_all(node, "symbol")
                    if len(sym) > 1 and str(sym[1])
                }
            nick = f.stem
            for name in names:
                ids.add(f"{nick}:{name}")
    return tuple(sorted(ids))


def symbol_index() -> tuple[str, ...]:
    """All available ``Lib:Name`` symbol IDs across the configured roots.

    Handles both the legacy single-file layout and the new per-symbol directory
    layout. Keyed on the current roots for correct test isolation."""
    return _symbol_index_for(tuple(str(r) for r in symbols.symbol_roots()))


def _names_in_lib(nick: str) -> list[tuple[str, str]]:
    """Return ``(name, full_id)`` pairs whose library nick matches (case-insensitive)."""
    low = nick.lower()
    out: list[tuple[str, str]] = []
    for full in symbol_index():
        lib, _, name = full.partition(":")
        if lib.lower() == low:
            out.append((name, full))
    return out


def ground_symbol(proposed: str) -> str | None:
    """Map ``proposed`` to a real symbol ID, or return it unchanged if already
    valid. Returns ``None`` only when no library is configured to check against."""
    if not proposed or ":" not in proposed:
        return proposed
    if symbols.resolve_symbol(proposed) is not None:
        return proposed
    if not symbol_index():  # no library configured — cannot ground
        return proposed

    nick, _, name = proposed.partition(":")
    a_nick = _LIB_ALIASES.get(nick.lower(), nick)
    a_name = _NAME_ALIASES.get(name.lower(), name)

    # Exact hit after aliasing.
    cand = f"{a_nick}:{a_name}"
    if symbols.resolve_symbol(cand) is not None:
        return cand

    # Prefer token-overlap scoring (precise for terse EE names): scope to the
    # aliased library first, then fall back to every library.
    scoped_ids = [full for _, full in (_names_in_lib(a_nick) or [])]
    if scoped_ids:
        best = _best_by_tokens(a_name, [(i, _tokens(i.partition(":")[2])) for i in scoped_ids])
        if best is not None:
            return best
    all_index = [(i, _tokens(i.partition(":")[2])) for i in symbol_index()]
    best = _best_by_tokens(f"{a_nick} {a_name}", all_index)
    if best is not None:
        return best
    # Fuzzy / substring as a last resort within the aliased library.
    scoped = _names_in_lib(a_nick) or [
        (full.partition(":")[2], full) for full in symbol_index()
    ]
    names = [n for n, _ in scoped]
    close = difflib.get_close_matches(a_name, names, n=1, cutoff=0.7)
    if close:
        return next(full for n, full in scoped if n == close[0])
    low = a_name.lower()
    subs = [
        (n, full) for n, full in scoped
        # Require a meaningful (>=3 char) overlap so a bogus name like
        # "DoesNotExist" cannot match a 1-char symbol ("D"/"R"/"C") just
        # because that letter happens to appear in it.
        if len(n) >= 3 and (low in n.lower() or n.lower() in low)
    ]
    if subs:  # shortest name containing the token is the most generic match
        subs.sort(key=lambda t: len(t[0]))
        return subs[0][1]
    return proposed  # leave as-is; the bottom-line check will block it


def ground_footprint(proposed: str) -> str | None:
    """Map ``proposed`` to a real footprint ID, or return it unchanged if valid.
    Empty input is passed through (footprint is optional at selection time)."""
    if not proposed:
        return proposed
    if footprints.footprint_pads(proposed) is not None:
        return proposed

    nick, _, name = proposed.partition(":") if ":" in proposed else ("", "", proposed)
    index = _footprint_index()
    if not index:
        return proposed
    query = f"{name}" if name else proposed
    # Prefer candidates in the proposed library, then search all libraries.
    same = [(i, set(t)) for i, t in index if i.partition(":")[0].lower() == nick.lower()]
    best = _best_by_tokens(query, same) if same else None
    if best is None:
        best = _best_by_tokens(query, [(i, set(t)) for i, t in index])
    if best is not None:
        return best
    # Last resort: the vendored substring search (handles odd stems).
    hits = search_footprints(name, limit=20) or search_footprints(
        name.split("_")[0], limit=20
    )
    return hits[0]["lib_id"] if hits else proposed
