"""Run one requirement file through the ratsnestpro multi-agent graph.

Streams the workflow phase events so the multi-agent progression is visible, then
writes the final Supervisor report. Used to exercise intent routing, capability
resolution and the AHE repair loop end to end against the real LLM.

    .\\scripts\\run_with_ericai.ps1 python scripts/run_ratsnest_case.py _case_stm32.txt
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import HumanMessage

from agents.ratsnestpro.ratsnestpro_agent import ratsnestpro_multi_agent


async def main(requirement_path: str, workflow_mode: str | None) -> int:
    requirement = Path(requirement_path).read_text(encoding="utf-8")
    thread_id = str(uuid4())
    configurable: dict[str, object] = {"thread_id": thread_id, "client_thread_id": thread_id}
    if workflow_mode:
        configurable["workflow_mode"] = workflow_mode
    config = {"configurable": configurable, "recursion_limit": 50}

    started = datetime.now(UTC)
    print(f"=== RUN START {started.isoformat()} ===", flush=True)
    print(f"requirement: {requirement_path} ({len(requirement)} chars)", flush=True)
    print(f"thread_id  : {thread_id}", flush=True)
    print(f"explicit workflow_mode: {workflow_mode or '(none, inferred)'}\n", flush=True)

    final_state: dict[str, object] = {}
    final_messages: list[object] = []
    async for mode, chunk in ratsnestpro_multi_agent.astream(
        {"messages": [HumanMessage(content=requirement)]},
        config=config,
        stream_mode=["custom", "updates"],
    ):
        if mode == "custom" and isinstance(chunk, dict) and chunk.get("kind") == "workflow_event":
            elapsed = (datetime.now(UTC) - started).total_seconds()
            detail = str(chunk.get("detail", ""))[:150]
            line = (
                f"[{elapsed:7.1f}s] {chunk.get('phase', ''):24} "
                f"{chunk.get('status', ''):18} {detail}"
            )
            if chunk.get("completed_steps") is not None:
                line += f"  ({chunk['completed_steps']}/{chunk.get('total_steps')})"
            print(line, flush=True)
        elif mode == "updates" and isinstance(chunk, dict):
            for node, update in chunk.items():
                if isinstance(update, dict):
                    messages = update.get("messages")
                    if isinstance(messages, list) and messages:
                        final_messages = messages
                    final_state.update(update)
                    keys = [k for k in update if k != "messages"]
                    print(f"          node={node} updated={keys}", flush=True)

    elapsed = (datetime.now(UTC) - started).total_seconds()
    print(f"\n=== RUN END after {elapsed:.1f}s ===", flush=True)

    # This graph is compiled without a checkpointer, so the streamed updates are
    # the authoritative record rather than aget_state.
    values = final_state
    report = getattr(final_messages[-1], "content", "") if final_messages else "(no report)"

    out_dir = Path("data/ratsnestpro/reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%d-%H%M%S")
    report_path = out_dir / f"run-{stamp}.md"
    report_path.write_text(str(report), encoding="utf-8")

    snapshot = {
        key: values.get(key)
        for key in (
            "workflow_mode",
            "intent",
            "component_constraints",
            "capability",
            "diagnosis",
            "repair_patches",
            "verification_digest",
            "verification_signatures",
            "change_evaluations",
            "run_name",
            "project_name",
            "review_target",
            "trace",
        )
    }
    architecture = values.get("architecture") or {}
    if isinstance(architecture, dict):
        snapshot["architecture_status"] = architecture.get("status")
        snapshot["symbol_acquisition"] = architecture.get("symbol_acquisition")
    hardware = values.get("hardware") or {}
    if isinstance(hardware, dict):
        snapshot["hardware"] = {
            k: hardware.get(k)
            for k in (
                "status",
                "completed_steps",
                "release_ready",
                "release_blockers",
                "actual_files",
                "routing",
            )
        }
        snapshot["hardware_steps"] = hardware.get("steps")
    state_path = out_dir / f"state-{stamp}.json"
    state_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    print(f"report : {report_path}")
    print(f"state  : {state_path}\n")
    print(str(report)[:6000])
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "_case_stm32.txt"
    mode = sys.argv[2] if len(sys.argv) > 2 else None
    raise SystemExit(asyncio.run(main(path, mode)))
