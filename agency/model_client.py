"""DeepSeek model adapter — bounded, validated, budget-aware.

Uses deepseek-v4-flash and deepseek-v4-pro model identifiers.
Validates output against JSON Schema using jsonschema.

No model call allowed when: DEEPSEEK_API_KEY absent, budget exhausted,
run stale, role deterministic, or policy forbids.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import jsonschema

# ---------------------------------------------------------------------------
# Model identifiers (current as of 2026-07-27)
# ---------------------------------------------------------------------------

DEFAULT_FLASH_MODEL = "deepseek-v4-flash"
DEFAULT_PRO_MODEL = "deepseek-v4-pro"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


def _get_api_key() -> str | None:
    return os.environ.get("DEEPSEEK_API_KEY")


# ---------------------------------------------------------------------------
# Model call result
# ---------------------------------------------------------------------------

@dataclass
class ModelCallResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    raw_json: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    error: str = ""
    error_kind: str = ""

    @property
    def failed(self) -> bool:
        return not self.success


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

FLASH_INPUT_COST = 0.07
FLASH_OUTPUT_COST = 0.14
PRO_INPUT_COST = 0.27
PRO_OUTPUT_COST = 1.10


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    if "pro" in model.lower():
        return (input_tokens * PRO_INPUT_COST + output_tokens * PRO_OUTPUT_COST) / 1_000_000
    return (input_tokens * FLASH_INPUT_COST + output_tokens * FLASH_OUTPUT_COST) / 1_000_000


# ---------------------------------------------------------------------------
# Model client
# ---------------------------------------------------------------------------

class DeepSeekClient:
    def __init__(self, flash_model: str = DEFAULT_FLASH_MODEL,
                 pro_model: str = DEFAULT_PRO_MODEL,
                 base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 60.0,
                 max_output_tokens: int = 4096,
                 transport: Callable[..., dict[str, Any]] | None = None) -> None:
        self.flash_model = flash_model
        self.pro_model = pro_model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self._transport = transport

    def call(self, model: str, system: str, user_context: dict[str, Any],
             schema: dict[str, Any] | None = None,
             temperature: float = 0.0) -> ModelCallResult:
        api_key = _get_api_key()
        if not api_key:
            return ModelCallResult(success=False, error="DEEPSEEK_API_KEY not set",
                                   error_kind="missing_key")

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_context, default=str)},
        ]

        payload: dict[str, Any] = {
            "model": model, "messages": messages,
            "temperature": temperature, "max_tokens": self.max_output_tokens,
        }
        if schema:
            payload["response_format"] = {"type": "json_object"}

        try:
            resp = self._transport(payload) if self._transport else self._http_call(payload)
        except Exception as exc:
            return ModelCallResult(success=False, error=str(exc), error_kind="transport")

        return self._parse_response(resp, schema)

    def _http_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        import urllib.request
        import urllib.error
        api_key = _get_api_key()
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"DeepSeek HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc
        except TimeoutError:
            raise RuntimeError(f"DeepSeek timeout after {self.timeout}s")

    def _parse_response(self, resp: dict[str, Any],
                        schema: dict[str, Any] | None) -> ModelCallResult:
        usage = resp.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", input_tokens + output_tokens)

        choices = resp.get("choices", [])
        if not choices:
            return ModelCallResult(success=False, error="Empty response",
                                   error_kind="empty", input_tokens=input_tokens,
                                   output_tokens=output_tokens, total_tokens=total_tokens)

        content = choices[0].get("message", {}).get("content", "")
        if not content.strip():
            return ModelCallResult(success=False, error="Empty content",
                                   error_kind="empty", input_tokens=input_tokens,
                                   output_tokens=output_tokens, total_tokens=total_tokens)

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            return ModelCallResult(success=False, error=f"Invalid JSON: {exc}",
                                   error_kind="invalid_json", raw_json=content,
                                   input_tokens=input_tokens, output_tokens=output_tokens,
                                   total_tokens=total_tokens)

        if schema:
            try:
                jsonschema.validate(instance=data, schema=schema)
            except jsonschema.ValidationError as exc:
                return ModelCallResult(success=False,
                                       error=f"Schema validation: {exc.message}",
                                       error_kind="schema", raw_json=content, data=data,
                                       input_tokens=input_tokens, output_tokens=output_tokens,
                                       total_tokens=total_tokens)

        model_used = resp.get("model", "")
        cost = estimate_cost(model_used or "unknown", input_tokens, output_tokens)
        return ModelCallResult(success=True, data=data, raw_json=content,
                               input_tokens=input_tokens, output_tokens=output_tokens,
                               total_tokens=total_tokens, estimated_cost=cost)


def validate_against_schema(instance: Any, schema: dict[str, Any]) -> list[str]:
    """Validate instance against JSON Schema. Returns list of error messages."""
    try:
        jsonschema.validate(instance=instance, schema=schema)
        return []
    except jsonschema.ValidationError as exc:
        return [exc.message]


# ---------------------------------------------------------------------------
# Role adapter
# ---------------------------------------------------------------------------

class RoleModelAdapter:
    def __init__(self, client: DeepSeekClient, model: str,
                 system_prompt: str, output_schema: dict[str, Any],
                 is_write_critical: bool = False) -> None:
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.output_schema = output_schema
        self.is_write_critical = is_write_critical

    def invoke(self, ctx_view: dict[str, Any]) -> ModelCallResult:
        result = self.client.call(self.model, self.system_prompt,
                                  ctx_view, self.output_schema)
        if result.success:
            return result
        if self.is_write_critical:
            return result  # fail immediately, no repair
        if result.error_kind in ("schema", "invalid_json", "empty"):
            repair = (
                f"{self.system_prompt}\n\n"
                f"Previous output was invalid: {result.error}\n"
                f"Produce valid JSON matching the schema."
            )
            return self.client.call(self.model, repair, ctx_view, self.output_schema)
        return result
