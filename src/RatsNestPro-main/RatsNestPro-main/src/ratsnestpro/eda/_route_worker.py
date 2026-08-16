"""KiCad-python routing worker (runs under KiCad's bundled interpreter, which
provides ``pcbnew``). Invoked as a subprocess by ``ratsnestpro.eda.routing``;
never imported into the main venv. Prints one ``RESULT <json>`` line.

Steps: load board -> assign nets to pads from a pinmap -> build connectivity ->
export Specctra DSN -> run Freerouting -> import the SES back -> save. Reports
pads assigned, tracks created, and remaining unconnected items (ratsnest).

Excluded from ruff/mypy in pyproject: it targets a foreign interpreter.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pcbnew


def _spacefree_dir(preferred):
    """A directory whose path Freerouting can actually be given.

    Freerouting v2.2.4 re-splits its own command-line arguments on whitespace
    instead of using the argv the OS handed it, so a path containing a space is
    truncated at the first one. Reproduced on a checkout under
    "OneDrive - Ericsson":

        WARN Unknown file type in -de argument: C:\\Users\\...\\OneDrive
        WARN Unknown command line argument: -
        WARN Unknown command line argument: Ericsson\\Desktop\\...\\board.dsn

    and the run then died inside ``RoutingJob.setInput`` ->
    ``FileInputStream.open`` with nothing in the stack naming the path as the
    cause. The same DSN copied to a space-free directory routes normally.

    Quoting cannot fix this: the OS-level argument is already intact when
    Freerouting splits it again. So the files have to live somewhere without a
    space. Returns ``preferred`` unchanged when it is already usable, and also
    when no space-free location can be found -- failing visibly is better than
    routing into a directory the caller cannot find afterwards.
    """
    if " " not in preferred:
        return preferred
    candidates = [
        tempfile.gettempdir(),
        os.environ.get("SystemDrive", "") + os.sep if os.environ.get("SystemDrive") else "",
    ]
    for base in candidates:
        if not base or " " in base or not os.path.isdir(base):
            continue
        try:
            made = tempfile.mkdtemp(prefix="rnp_fr_", dir=base)
        except OSError:
            continue
        if " " not in made:
            return made
        shutil.rmtree(made, ignore_errors=True)
    return preferred


def _router_timeout(layer_count):
    default = 3600 if layer_count >= 4 else 1800
    raw = os.environ.get("RATSNESTPRO_ROUTER_TIMEOUT_SECONDS", "")
    try:
        requested = int(raw) if raw else default
    except ValueError:
        requested = default
    return max(300, min(requested, 7200))


def _apply_default_netclass(
    board,
    clearance_mm,
    track_width_mm,
    via_diameter_mm,
    via_drill_mm,
):
    netclass = board.GetAllNetClasses()["Default"]
    netclass.SetClearance(pcbnew.FromMM(clearance_mm))
    netclass.SetTrackWidth(pcbnew.FromMM(track_width_mm))
    netclass.SetViaDiameter(pcbnew.FromMM(via_diameter_mm))
    netclass.SetViaDrill(pcbnew.FromMM(via_drill_mm))
    board.GetNetClasses()["Default"] = netclass
    # Keep KiCad's authoritative DRC boundary rules aligned with the verified
    # route rule used by the DSN/Freerouting execution. The minimum track width
    # matters as much as the edge clearance: KiCad defaults it to 0.2 mm, so a
    # thinner verified route rule makes DRC reject every track it just routed.
    board.GetDesignSettings().m_CopperEdgeClearance = pcbnew.FromMM(clearance_mm)
    board.GetDesignSettings().m_TrackMinWidth = pcbnew.FromMM(track_width_mm)
    board.SynchronizeNetsAndNetClasses(False)


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _assign_nets(board, netmap):
    """Assign nets and return physical-pad count plus matched logical pin keys."""
    name_to_net = {}
    for name in netmap:
        net = board.FindNet(name)
        if net is None:
            net = pcbnew.NETINFO_ITEM(board, name)
            board.Add(net)
        name_to_net[name] = net
    pad_net = {}
    for name, pins in netmap.items():
        for ref, pad in pins:
            pad_net[(str(ref), str(pad))] = name
    assigned = 0
    matched = set()
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            key = (ref, pad.GetNumber())
            if key in pad_net:
                pad.SetNet(name_to_net[pad_net[key]])
                assigned += 1
                matched.add(key)
    board.BuildConnectivity()
    return assigned, len(matched)


def _import_ses(board, ses):
    try:
        return pcbnew.ImportSpecctraSES(board, ses)
    except TypeError:
        return pcbnew.ImportSpecctraSES(ses)


def _track_count(board):
    try:
        return int(board.GetTracks().size())
    except Exception:
        return len(list(board.GetTracks()))


def _unconnected(board):
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    for args in ((True,), ()):
        try:
            return int(conn.GetUnconnectedCount(*args))
        except Exception:
            continue
    return -1


def main():
    pcb, netmap_json, fr_exe, workdir = sys.argv[1:5]
    max_passes = sys.argv[5] if len(sys.argv) > 5 else "20"
    layer_count = int(sys.argv[6]) if len(sys.argv) > 6 else 2
    clearance_mm = float(sys.argv[7]) if len(sys.argv) > 7 else 0.2
    track_width_mm = float(sys.argv[8]) if len(sys.argv) > 8 else 0.2
    via_diameter_mm = float(sys.argv[9]) if len(sys.argv) > 9 else 0.6
    via_drill_mm = float(sys.argv[10]) if len(sys.argv) > 10 else 0.3

    stem = os.path.splitext(os.path.basename(pcb))[0]
    # Freerouting is handed paths inside ``fr_dir``, which is ``workdir`` unless
    # that contains a space (see ``_spacefree_dir``). The reported paths stay in
    # ``workdir`` either way, and the artifacts are copied back, so a caller
    # archiving the run finds them where it expects.
    fr_dir = _spacefree_dir(workdir)
    dsn = os.path.join(fr_dir, stem + ".dsn")
    ses = os.path.join(fr_dir, stem + ".ses")
    reported_dsn = os.path.join(workdir, stem + ".dsn")
    reported_ses = os.path.join(workdir, stem + ".ses")
    result = {
        "assigned": 0,
        "matched_logical_pins": 0,
        "expected_pads": 0,
        "routed_tracks": 0,
        "unconnected": -1,
        "fr_ok": False,
        "layers": layer_count,
        "error": "",
        "fr_tail": "",
        "dsn_path": reported_dsn,
        "ses_path": reported_ses,
        "router_workdir_relocated": fr_dir != workdir,
    }
    try:
        netmap = _load(netmap_json)
        result["nets"] = len(netmap)
        logical_keys = {}
        conflicting_keys = []
        for net_name, pins in netmap.items():
            for ref, pad in pins:
                key = (str(ref), str(pad))
                previous = logical_keys.get(key)
                if previous is not None and previous != net_name:
                    conflicting_keys.append(
                        f"{key[0]}:{key[1]} in {previous} & {net_name}"
                    )
                logical_keys[key] = net_name
        if conflicting_keys:
            raise RuntimeError(
                f"logical pins assigned to multiple nets: {conflicting_keys}"
            )
        result["expected_pads"] = len(logical_keys)
        board = pcbnew.LoadBoard(pcb)
        if layer_count >= 2:
            board.SetCopperLayerCount(layer_count)
        result["assigned"], result["matched_logical_pins"] = _assign_nets(
            board, netmap
        )
        _apply_default_netclass(
            board,
            clearance_mm,
            track_width_mm,
            via_diameter_mm,
            via_drill_mm,
        )
        if result["matched_logical_pins"] != result["expected_pads"]:
            raise RuntimeError(
                "pin-map/footprint mismatch: matched "
                f"{result['matched_logical_pins']}/{result['expected_pads']} "
                f"logical pins ({result['assigned']} physical pads assigned)"
            )
        pcbnew.SaveBoard(pcb, board)  # persist connectivity

        pcbnew.ExportSpecctraDSN(board, dsn)

        proc = subprocess.run(
            [fr_exe, "-de", dsn, "-do", ses, "-mp", str(max_passes)],
            capture_output=True,
            text=True,
            # Freerouting's banner is not valid UTF-8 under a non-UTF-8 console
            # codepage; without this the reader thread dies and stdout is None.
            encoding="utf-8",
            errors="replace",
            timeout=_router_timeout(layer_count),
        )
        combined = "\n".join(
            ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines()[-6:]
        )
        result["fr_tail"] = combined
        if proc.returncode == 0 and os.path.exists(ses) and os.path.getsize(ses) > 0:
            board2 = pcbnew.LoadBoard(pcb)  # reload (carries nets)
            _import_ses(board2, ses)
            result["unconnected"] = _unconnected(board2)
            pcbnew.SaveBoard(pcb, board2)
            result["routed_tracks"] = _track_count(board2)
            result["fr_ok"] = True
        else:
            result["error"] = (
                f"Freerouting failed (exit={proc.returncode}); tail={combined!r}"
            )
    except Exception as exc:  # noqa: BLE001 - report back to the caller
        result["error"] = f"{type(exc).__name__}: {exc}"
    if fr_dir != workdir:
        # Copy the router's inputs and outputs to where the caller reported them,
        # so a failed run is still diagnosable from the run directory.
        for src, dest in ((dsn, reported_dsn), (ses, reported_ses)):
            try:
                if os.path.exists(src):
                    shutil.copy2(src, dest)
            except OSError:
                pass
        shutil.rmtree(fr_dir, ignore_errors=True)
    print("RESULT " + json.dumps(result))


# Guarded so the module can be imported for testing. Importing it under the main
# venv needs a ``pcbnew`` stub; ``_spacefree_dir`` is pure path logic and is what
# the tests are after.
if __name__ == "__main__":
    main()
