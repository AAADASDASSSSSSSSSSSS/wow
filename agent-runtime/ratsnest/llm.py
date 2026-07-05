"""The agent brain: multi-provider LLM client with typed-contract discipline.

Providers (RATSNEST_LLM_PROVIDER):
  anthropic   Anthropic Messages API (default)
  openai      any OpenAI-compatible /chat/completions endpoint — covers
              DeepSeek, Qwen/DashScope, Moonshot/Kimi, GLM/Zhipu, vLLM, ...
  ollama      local Ollama (openai protocol, no key required)
Presets fill base URLs for: deepseek, qwen, moonshot, zhipu, ollama.

Architecture invariant: the LLM PROPOSES, tools EXECUTE, checkers VERIFY,
AHE evolves, the control plane governs. Every call is an ATDP event
(`llm.<agent>`); callers validate every completion against a Pydantic
contract and fall back to their deterministic path — unless
RATSNEST_LLM=require, in which case a missing/failed brain raises instead of
degrading (for deployments that must be LLM-driven end to end).
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

# provider presets: (protocol, default_base_url, default_model)
PROVIDERS: dict[str, tuple[str, str, str]] = {
    "anthropic": ("anthropic", "https://api.anthropic.com", "claude-sonnet-5"),
    "openai": ("openai", "https://api.openai.com", "gpt-4o-mini"),
    "deepseek": ("openai", "https://api.deepseek.com", "deepseek-chat"),
    "qwen": ("openai",
             "https://dashscope.aliyuncs.com/compatible-mode", "qwen-plus"),
    "moonshot": ("openai", "https://api.moonshot.cn", "moonshot-v1-8k"),
    "zhipu": ("openai", "https://open.bigmodel.cn/api/paas", "glm-4-plus"),
    "ollama": ("openai", "http://localhost:11434", "llama3.1"),
}


class BrainRequiredError(RuntimeError):
    """Raised in require mode when the brain is unavailable or fails."""


class LlmClient:
    def __init__(self, config: Config | None = None,
                 recorder: Recorder | None = None,
                 iteration: int = 0, timeout: float = 120.0):
        self.config = config or Config.load()
        self.recorder = recorder
        self.iteration = iteration
        self.timeout = timeout
        preset = PROVIDERS.get(self.config.llm_provider,
                               PROVIDERS["anthropic"])
        self.protocol = preset[0]
        self.base_url = (self.config.llm_base_url or preset[1]).rstrip("/")
        self.model = self.config.llm_model or preset[2]

    @property
    def available(self) -> bool:
        if not self.config.llm_enabled or httpx is None:
            return False
        if self.config.llm_provider == "ollama":
            return True  # local, keyless
        return bool(self.config.llm_api_key)

    @property
    def required(self) -> bool:
        return self.config.llm_required

    def complete_json(self, agent: str, system: str, user: str,
                      max_tokens: int = 2000) -> dict[str, Any] | None:
        """One brain invocation -> parsed JSON dict, or None on failure
        (raises BrainRequiredError instead when RATSNEST_LLM=require)."""
        if not self.available:
            if self.required:
                raise BrainRequiredError(
                    f"RATSNEST_LLM=require but no usable brain "
                    f"(provider={self.config.llm_provider}, key set: "
                    f"{bool(self.config.llm_api_key)})")
            return None
        started = time.monotonic()
        error: str | None = None
        usage: dict[str, Any] = {}
        parsed: dict[str, Any] | None = None
        try:
            if self.protocol == "anthropic":
                text, usage, error = self._call_anthropic(
                    system, user, max_tokens)
            else:
                text, usage, error = self._call_openai(
                    system, user, max_tokens)
            if error is None:
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
                    action={"provider": self.config.llm_provider,
                            "model": self.model,
                            "system_chars": len(system),
                            "prompt_chars": len(user)},
                    outcome={"ok": error is None, "error": error,
                             "usage": usage,
                             "elapsed_s": round(time.monotonic() - started, 2)},
                    metadata={"agent": agent, "crew": "brain"},
                )
        if parsed is None and self.required:
            raise BrainRequiredError(
                f"brain call failed for {agent}: {error}")
        return parsed

    # -- protocol adapters -----------------------------------------------------

    def _call_anthropic(self, system: str, user: str, max_tokens: int):
        response = httpx.post(
            f"{self.base_url}/v1/messages",
            headers={"x-api-key": self.config.llm_api_key or "",
                     "authorization": f"Bearer {self.config.llm_api_key}",
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.model, "max_tokens": max_tokens,
                  "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=self.timeout)
        if response.status_code != 200:
            return "", {}, f"http {response.status_code}: {response.text[:200]}"
        data = response.json()
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        return text, data.get("usage", {}), None

    def _call_openai(self, system: str, user: str, max_tokens: int):
        headers = {"content-type": "application/json"}
        if self.config.llm_api_key:
            headers["authorization"] = f"Bearer {self.config.llm_api_key}"
        response = httpx.post(
            f"{self.base_url}/v1/chat/completions",
            headers=headers,
            json={"model": self.model, "max_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}]},
            timeout=self.timeout)
        if response.status_code != 200:
            return "", {}, f"http {response.status_code}: {response.text[:200]}"
        data = response.json()
        choices = data.get("choices") or []
        text = (choices[0].get("message", {}).get("content", "")
                if choices else "")
        return text, data.get("usage", {}), None


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
