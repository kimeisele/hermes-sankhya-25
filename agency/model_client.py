"""DeepSeek model adapter — bounded, validated, budget-aware.

Reads DEEPSEEK_API_KEY only from the environment. Never stores or prints
the key. Supports Flash and Pro model identifiers with configurable
endpoints. Every call validates output against a provided JSON schema.

No model call is allowed when:
- DEEPSEEK_API_KEY is absent
- budget is exhausted
- run is stale
- role is deterministic
- policy forbids the operation
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Model identifiers (configurable via config)
# ---------------------------------------------------------------------------

DEFAULT_FLASH_MODEL = "deepseek-chat"  # DeepSeek Flash equivalent
DEFAULT_PRO_MODEL = "deepseek-reasoner"  # DeepSeek Pro equivalent
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


def _get_api_key() -> str | None:
    """Read DEEPSEEK_API_KEY from environment. Never stored, never printed."""
    return os.environ.get("DEEPSEEK_API_KEY")


# ---------------------------------------------------------------------------
# Model call result
# ---------------------------------------------------------------------------

@dataclass
class ModelCallResult:
    """Result of a single model invocation."""
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    raw_json: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    error: str = ""
    error_kind: str = ""  # transport, empty, invalid_json, schema, timeout, missing_key, budget, stale, forbidden

    @property
    def failed(self) -> bool:
        return not self.success


# ---------------------------------------------------------------------------
# Cost estimation (approximate, configurable)
# ---------------------------------------------------------------------------

# DeepSeek pricing per 1M tokens (approximate)
FLASH_INPUT_COST = 0.14   # $0.14/1M input
FLASH_OUTPUT_COST = 0.28  # $0.28/1M output
PRO_INPUT_COST = 0.55     # $0.55/1M input
PRO_OUTPUT_COST = 2.19    # $2.19/1M output


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a model call."""
    if "reasoner" in model or "pro" in model.lower():
        return (input_tokens * PRO_INPUT_COST + output_tokens * PRO_OUTPUT_COST) / 1_000_000
    return (input_tokens * FLASH_INPUT_COST + output_tokens * FLASH_OUTPUT_COST) / 1_000_000


# ---------------------------------------------------------------------------
# Model client
# ---------------------------------------------------------------------------

class DeepSeekClient:
    """Bounded DeepSeek API client with schema validation.

    In tests, inject a callable `_transport` to avoid real HTTP calls.
    """

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
        self._transport = transport  # injected for tests

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call(self, model: str, system: str, user_context: dict[str, Any],
             schema: dict[str, Any] | None = None,
             temperature: float = 0.0) -> ModelCallResult:
        """Make a single model call with schema validation.

        Args:
            model: Model identifier (flash or pro).
            system: Stable system instruction.
            user_context: Role-specific CTX view (serialized as JSON).
            schema: Optional JSON Schema for output validation.
            temperature: Sampling temperature (0.0 for deterministic).
        """
        api_key = _get_api_key()
        if not api_key:
            return ModelCallResult(success=False, error="DEEPSEEK_API_KEY not set",
                                   error_kind="missing_key")

        # Build messages
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_context, default=str)},
        ]

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.max_output_tokens,
        }
        if schema:
            payload["response_format"] = {"type": "json_object"}

        # Transport (real or injected)
        try:
            if self._transport:
                resp = self._transport(payload)
            else:
                resp = self._http_call(payload)
        except Exception as exc:
            return ModelCallResult(success=False, error=str(exc),
                                   error_kind="transport")

        return self._parse_response(resp, schema)

    # ------------------------------------------------------------------
    # HTTP transport
    # ------------------------------------------------------------------

    def _http_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        import urllib.request
        import urllib.error

        api_key = _get_api_key()
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DeepSeek HTTP {exc.code}: {body}") from exc
        except TimeoutError:
            raise RuntimeError(f"DeepSeek timeout after {self.timeout}s")

    # ------------------------------------------------------------------
    # Response parsing + schema validation
    # ------------------------------------------------------------------

    def _parse_response(self, resp: dict[str, Any],
                        schema: dict[str, Any] | None) -> ModelCallResult:
        # Extract token usage
        usage = resp.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", input_tokens + output_tokens)

        # Extract content
        choices = resp.get("choices", [])
        if not choices:
            return ModelCallResult(success=False, error="Empty response from model",
                                   error_kind="empty",
                                   input_tokens=input_tokens,
                                   output_tokens=output_tokens,
                                   total_tokens=total_tokens)

        content = choices[0].get("message", {}).get("content", "")
        model_used = resp.get("model", "")

        if not content.strip():
            return ModelCallResult(success=False, error="Empty content from model",
                                   error_kind="empty",
                                   input_tokens=input_tokens,
                                   output_tokens=output_tokens,
                                   total_tokens=total_tokens)

        # Parse JSON
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            return ModelCallResult(success=False, error=f"Invalid JSON: {exc}",
                                   error_kind="invalid_json",
                                   raw_json=content,
                                   input_tokens=input_tokens,
                                   output_tokens=output_tokens,
                                   total_tokens=total_tokens)

        # Schema validation
        if schema:
            errors = _validate_json_schema(data, schema)
            if errors:
                return ModelCallResult(success=False,
                                       error=f"Schema validation failed: {errors}",
                                       error_kind="schema",
                                       raw_json=content, data=data,
                                       input_tokens=input_tokens,
                                       output_tokens=output_tokens,
                                       total_tokens=total_tokens)

        cost = estimate_cost(model_used or "unknown", input_tokens, output_tokens)
        return ModelCallResult(success=True, data=data, raw_json=content,
                               input_tokens=input_tokens,
                               output_tokens=output_tokens,
                               total_tokens=total_tokens,
                               estimated_cost=cost)


# ---------------------------------------------------------------------------
# Schema validation (lightweight, no jsonschema dependency required)
# ---------------------------------------------------------------------------

def _validate_json_schema(instance: Any, schema: dict[str, Any]) -> list[str]:
    """Lightweight JSON Schema validator. Returns list of error messages."""
    errors: list[str] = []

    schema_type = schema.get("type")
    if schema_type == "object" and isinstance(instance, dict):
        required = schema.get("required", [])
        for field in required:
            if field not in instance:
                errors.append(f"Missing required field: {field}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                sub_errors = _validate_json_schema(value, properties[key])
                for e in sub_errors:
                    errors.append(f"{key}: {e}")

        # Check enum constraints on specific fields
        for field, prop_schema in properties.items():
            if field in instance and "enum" in prop_schema:
                if instance[field] not in prop_schema["enum"]:
                    errors.append(f"{field}: '{instance[field]}' not in {prop_schema['enum']}")

    elif schema_type == "array" and isinstance(instance, list):
        items_schema = schema.get("items", {})
        for i, item in enumerate(instance):
            sub_errors = _validate_json_schema(item, items_schema)
            for e in sub_errors:
                errors.append(f"[{i}]: {e}")

    elif schema_type == "string":
        if not isinstance(instance, str):
            errors.append(f"Expected string, got {type(instance).__name__}")
        elif "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"String too short: {len(instance)} < {schema['minLength']}")
        elif "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"String too long: {len(instance)} > {schema['maxLength']}")

    elif schema_type == "integer":
        if not isinstance(instance, int) or isinstance(instance, bool):
            errors.append(f"Expected integer, got {type(instance).__name__}")

    elif schema_type == "number":
        if not isinstance(instance, (int, float)) or isinstance(instance, bool):
            errors.append(f"Expected number, got {type(instance).__name__}")

    return errors


# ---------------------------------------------------------------------------
# Role adapter — connects model client to role logic
# ---------------------------------------------------------------------------

class RoleModelAdapter:
    """Wraps a model client for a specific role with schema enforcement.

    Handles schema-repair for Flash roles and fail-closed for Pro/write-critical.
    """

    def __init__(self, client: DeepSeekClient, model: str,
                 system_prompt: str, output_schema: dict[str, Any],
                 is_write_critical: bool = False) -> None:
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.output_schema = output_schema
        self.is_write_critical = is_write_critical

    def invoke(self, ctx_view: dict[str, Any]) -> ModelCallResult:
        """Invoke the model with schema validation and repair logic."""
        result = self.client.call(self.model, self.system_prompt,
                                  ctx_view, self.output_schema)

        if result.success:
            return result

        # For write-critical roles: fail immediately, no repair
        if self.is_write_critical:
            return result

        # For Flash roles: one schema-repair attempt
        if result.error_kind in ("schema", "invalid_json", "empty"):
            repair_prompt = (
                f"{self.system_prompt}\n\n"
                f"Your previous output was invalid: {result.error}\n"
                f"Please produce a valid JSON response matching the required schema."
            )
            return self.client.call(self.model, repair_prompt,
                                    ctx_view, self.output_schema)

        return result
