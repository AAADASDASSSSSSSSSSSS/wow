"""The agent brain: LLM client with typed-contract discipline.

Architecture invariant (design doc): the LLM PROPOSES, tools EXECUTE,
checkers VERIFY, AHE evolves, the control plane governs. Concretely:

- every call returns JSON that the caller validates against a Pydantic
  contract; invalid output is dropped, never improvised around
- every call is an ATDP event (`llm.<agent>`) with model, token usage and
  outcome — LLM reasoning is trajectory data, same as tool calls
- when no key is configured (or the call fails) callers fall back to their
  deterministic path and record which brain produced the decision
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ratsnest.config import Config
from ratsnest.data_proxy import Recorder

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


class LlmClient:
    def __init__(self, config: Config | None = None,
                 recorder: Recorder | None = None,
                 iteration: int = 0, timeout: float = 120.0):
        self.config = config or Config.load()
        self.recorder = recorder
        self.iteration = iteration
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.config.llm_enabled and self.config.llm_api_key
                    and httpx is not None)

    def complete_json(self, agent: str, system: str, user: str,
                      max_tokens: int = 2000) -> dict[str, Any] | None:
        """One brain invocation -> parsed JSON dict, or None on any failure."""
        if not self.available:
            return None
        started = time.monotonic()
        error: str | None = None
        usage: dict[str, Any] = {}
        parsed: dict[str, Any] | None = None
        try:
            response = httpx.post(
                f"{self.config.llm_base_url.rstrip('/')}/v1/messages",
                headers={
                    "x-api-key": self.config.llm_api_key,
                    "authorization": f"Bearer {self.config.llm_api_key}",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.config.llm_model,
                    "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                timeout=self.timeout,
            )
            if response.status_code != 200:
                error = f"http {response.status_code}: {response.text[:200]}"
            else:
                data = response.json()
                usage = data.get("usage", {})
                text = "".join(block.get("text", "")
                               for block in data.get("content", [])
                               if block.get("type") == "text")
                parsed = extract_json(text)
                if parsed is None:
                    error = "no JSON object in completion"
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            if self.recorder is not None:
                self.recorder.emit(
                    f"llm.{agent}", self.iteration,
                    agent_state={"brain": "llm"},
                    action={"model": self.config.llm_model,
                            "system_chars": len(system),
                            "prompt_chars": len(user)},
                    outcome={"ok": error is None, "error": error,
                             "usage": usage,
                             "elapsed_s": round(time.monotonic() - started, 2)},
                    metadata={"agent": agent, "crew": "brain"},
                )
        return parsed


def extract_json(text: str) -> dict[str, Any] | None:
    """Parse the first JSON object out of a completion (fences tolerated)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start:i + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None
