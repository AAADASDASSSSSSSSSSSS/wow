"""Kafka run worker — the cluster-mode replacement for local dispatch.

Consumes run requests from `ratsnest.run-requests`, executes them with the
same pipeline as the CLI (design generation or repair loop), and PUTs the
RunRecord back to the control plane's callback URL. ATDP events stream to the
control plane during execution exactly as in local mode.

    python -m ratsnest.worker            (env: RATSNEST_KAFKA=host:9092)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from ratsnest.config import Config
from ratsnest.schemas import RunConfig

RUN_REQUEST_TOPIC = os.environ.get("RATSNEST_TOPIC_RUNS", "ratsnest.run-requests")
GROUP_ID = os.environ.get("RATSNEST_WORKER_GROUP", "ratsnest-workers")


def handle_request(msg: dict, config: Config) -> None:
    from ratsnest.orchestrator import RunLoop

    kind = msg.get("kind", "fix")
    project_dir = msg["projectDir"]
    max_iter = int(msg.get("maxIterations", 4))
    # ATDP events flow to the control plane during the run
    if msg.get("controlPlaneUrl"):
        config.control_plane_url = msg["controlPlaneUrl"]

    spec = None
    strategy = None
    if kind == "design":
        from ratsnest.evolution import StrategyRegistry
        from ratsnest.pipeline import generate_for_backend
        _, strategy = StrategyRegistry(config.strategies_dir).load_active()
        spec = generate_for_backend(
            msg.get("requirement", ""), Path(project_dir),
            msg.get("backend", "template"), strategy, config)

    record = RunLoop(config).execute(RunConfig(
        project_dir=project_dir, max_iterations=max_iter, run_erc=False))

    # deliverables so the control plane can serve download + previews
    if kind == "design":
        try:
            from ratsnest.agents import synthesize
            from ratsnest.kh_adapter import KicadHappyAdapter
            from ratsnest.pipeline import finalize_outputs
            ev = synthesize(KicadHappyAdapter(config).analyze_all(
                Path(project_dir)), strategy, project_dir)
            finalize_outputs(Path(project_dir), ev, record, spec, config)
        except Exception as exc:
            print(f"[worker] finalize failed: {exc}", flush=True)

    callback = msg.get("callbackUrl")
    if callback:
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("RATSNEST_SERVICE_TOKEN")
        if token:
            headers["X-RatsNest-Service-Token"] = token
        httpx.put(callback, content=record.model_dump_json(),
                  headers=headers, timeout=30)
    print(f"[worker] run {msg.get('runId')} -> {record.status}", flush=True)


def main() -> None:
    try:
        from kafka import KafkaConsumer  # kafka-python
    except ImportError as exc:
        raise SystemExit(
            "kafka-python not installed — pip install kafka-python "
            "(cluster mode only)") from exc

    config = Config.load()
    bootstrap = os.environ.get("RATSNEST_KAFKA", "localhost:9092")
    consumer = KafkaConsumer(
        RUN_REQUEST_TOPIC,
        bootstrap_servers=bootstrap,
        group_id=GROUP_ID,
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        enable_auto_commit=True,
        auto_offset_reset="earliest",
    )
    print(f"[worker] consuming {RUN_REQUEST_TOPIC} from {bootstrap}", flush=True)
    for message in consumer:
        try:
            handle_request(message.value, config)
        except Exception as exc:  # a bad run must not kill the worker
            print(f"[worker] run failed: {exc}", flush=True)
            callback = (message.value or {}).get("callbackUrl")
            if callback:
                try:
                    httpx.put(callback, json={"status": "failed",
                                              "error": str(exc)[:500]},
                              timeout=10)
                except Exception:
                    pass


if __name__ == "__main__":
    main()
