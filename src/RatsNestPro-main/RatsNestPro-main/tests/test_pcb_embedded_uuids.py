from ratsnestpro.eda.vendor.pcb import PcbBoard
from ratsnestpro.eda.vendor.sexpr import find_all, find_first, loads, tag_of


def _uuid_values(node) -> list[str]:
    if not isinstance(node, list):
        return []
    values = [str(node[1])] if tag_of(node) == "uuid" and len(node) > 1 else []
    for child in node:
        values.extend(_uuid_values(child))
    return values


def test_reused_library_footprints_get_unique_embedded_uuids() -> None:
    footprint = loads(
        """
        (footprint "Test:Part"
          (layer "F.Cu")
          (fp_line
            (start 0 0)
            (end 1 0)
            (stroke (width 0.1) (type default))
            (layer "F.SilkS")
            (uuid "11111111-1111-1111-1111-111111111111"))
          (pad "1" smd rect
            (at 0 0)
            (size 1 1)
            (layers "F.Cu")
            (uuid "22222222-2222-2222-2222-222222222222")))
        """
    )
    board = PcbBoard.blank()

    board.add_footprint(
        "Test:Part",
        "R1",
        "1k",
        10,
        10,
        rotation=90,
        embed_node=footprint,
    )
    board.add_footprint("Test:Part", "R2", "1k", 20, 10, embed_node=footprint)

    uuids = _uuid_values(board.root)
    assert len(uuids) == len(set(uuids))
    first_pad = find_all(find_all(board.root, "footprint")[0], "pad")[0]
    assert str(find_first(first_pad, "at")[3]) == "90"
