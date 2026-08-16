"""Task 11: board partition & outline planning + bottom-line (zones in bounds)."""

from __future__ import annotations

import json

from ratsnestpro.agents.llm import LlmMode
from ratsnestpro.orchestration.pipeline import (
    LayoutPartitionStep,
    PipelineContext,
    PipelineState,
    PipelineStep,
)
from ratsnestpro.orchestration.pipeline_contracts import BoardPartition


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    def complete(self, system, user, temperature=0.2):
        return self._responses.pop(0) if self._responses else "{}"


def test_offline_partition_within_board() -> None:
    state = PipelineState(requirement_text="ATmega328 dev board 3.3V 8MHz")
    LayoutPartitionStep().run(state, PipelineContext(mode=LlmMode.OFFLINE))
    part = state.artifact(PipelineStep.LAYOUT_PARTITION)
    assert isinstance(part, BoardPartition)
    assert part.board_width > 0 and part.board_height > 0
    assert part.zones
    result = state.results[-1]
    assert not result.blocked
    assert all(c.ok for c in result.checks)


def test_zone_out_of_bounds_blocks() -> None:
    bad = json.dumps({
        "board_width": 40, "board_height": 30,
        "zones": [{"name": "z", "kind": "power", "x1": 0, "y1": 0, "x2": 60, "y2": 20}],
    })
    state = PipelineState(requirement_text="x")
    LayoutPartitionStep().run(state, PipelineContext(mode=LlmMode.AUTO, client=FakeLLM([bad])))
    result = state.results[-1]
    assert result.blocked
    assert any(c.name == "zones_within_board" and not c.ok for c in result.checks)
