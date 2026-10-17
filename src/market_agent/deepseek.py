"""Explicit DeepSeek Chat Completions adapter; no fallback or URL override."""

import json
from contextvars import ContextVar
from time import perf_counter

import httpx
from pydantic import BaseModel, ValidationError

from .llm import LLMConfigurationError, LLMError, StructuredLLMRequest


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODELS = ("deepseek-flash", "deepseek-v4-pro")


class DeepSeekLLMClient:
    provider = "deepseek"
    supported_capabilities = ("json_object", "typed_tool_arguments", "non_thinking")

    def __init__(self, api_key: str, model: str, *,
                 client: httpx.AsyncClient | None = None,
                 timeout: float = 30.0, max_tokens: int = 1024):
        if model not in DEEPSEEK_MODELS or not api_key.strip():
            raise LLMConfigurationError("configuration", "DeepSeek requires its own key and an allowed model")
        if not 1 <= max_tokens <= 8192 or not 0 < timeout <= 120:
            raise LLMConfigurationError("configuration", "DeepSeek request bounds are invalid")
        self._api_key = api_key.strip()
        self.model_name = model
        self.client = client
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._metadata: ContextVar[dict] = ContextVar("deepseek_call_metadata", default={})

    def call_metadata(self) -> dict:
        return dict(self._metadata.get())

    async def complete_structured(self, request: StructuredLLMRequest) -> BaseModel:
        prompt = {
            "purpose": request.purpose, "facts": request.facts,
            "allowed_evidence_ids": request.evidence_ids,
            "allowed_numeric_tokens": request.numeric_fact_tokens,
            "response_schema": request.response_model.model_json_schema(),
        }
        system = (request.system_instruction or
                  "Return one JSON object matching response_schema using only supplied facts and evidence IDs. "
                  "Do not calculate or invent numeric facts.")
        if request.generation_mode == "select":
            prompt["allowed_grounded_responses"] = request.grounded_responses
            system = "Select one JSON object from allowed_grounded_responses unchanged."
        return await self._complete({
            "messages": [{"role": "system", "content": system + " Return JSON only."},
                         {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)}],
            "response_format": {"type": "json_object"},
        }, request.response_model)

    async def complete_tool_arguments(self, *, prompt: str, name: str,
                                      arguments_model: type[BaseModel]) -> BaseModel:
        """Validate a proposed tool call; this method never executes a tool."""
        if not name.isidentifier() or len(name) > 128:
            raise LLMConfigurationError("configuration", "Invalid tool name")
        return await self._complete({
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "function", "function": {
                "name": name, "description": "Propose typed research arguments. No trade execution.",
                "parameters": arguments_model.model_json_schema(),
            }}],
            "tool_choice": {"type": "function", "function": {"name": name}},
        }, arguments_model, tool_name=name)

    async def _complete(self, payload: dict, response_model: type[BaseModel], *,
                        tool_name: str | None = None) -> BaseModel:
        metadata = {"provider": self.provider, "model": self.model_name,
                    "supported_capabilities": list(self.supported_capabilities),
                    "usage": {}, "error_type": None, "validated_output": False,
                    "schema_valid": False, "tool_call_valid": None if tool_name is None else False}
        self._metadata.set(metadata)
        started = perf_counter()
        payload = {**payload, "model": self.model_name, "stream": False,
                   "thinking": {"type": "disabled"}, "max_tokens": self.max_tokens}
        try:
            if self.client is None:
                async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
                    response = await self._post(client, payload)
            else:
                response = await self._post(self.client, payload)
            if response.status_code != 200:
                code = ("authentication" if response.status_code in (401, 403) else
                        "credit_exhausted" if response.status_code == 402 else
                        "rate_limit" if response.status_code == 429 else "http_error")
                raise LLMError(code, f"DeepSeek HTTP {response.status_code}")
            try:
                body = response.json()
                if body.get("model") != self.model_name:
                    raise LLMError("model_mismatch", "DeepSeek returned a different model")
                usage = body.get("usage", {})
                if isinstance(usage, dict):
                    metadata["usage"] = {k: v for k, v in usage.items()
                                         if k in {"prompt_tokens", "completion_tokens", "total_tokens",
                                                  "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
                                         and type(v) is int and v >= 0}
                choice = body["choices"][0]
                finish = choice["finish_reason"]
                if finish == "length":
                    raise LLMError("truncated", "DeepSeek output exceeded token limit")
                if finish != ("tool_calls" if tool_name else "stop"):
                    raise LLMError("finish_reason", "DeepSeek did not complete the requested output")
                message = choice["message"]
                if tool_name:
                    calls = message["tool_calls"]
                    if len(calls) != 1 or calls[0]["type"] != "function" or calls[0]["function"]["name"] != tool_name:
                        raise LLMError("tool_call", "DeepSeek returned an unexpected tool call")
                    content = calls[0]["function"]["arguments"]
                else:
                    if message.get("tool_calls"):
                        raise LLMError("tool_call", "Unexpected tool call in JSON output")
                    content = message["content"]
                if not isinstance(content, str) or not content.strip():
                    raise LLMError("empty_content", "DeepSeek returned empty output")
                result = response_model.model_validate_json(content)
            except ValidationError:
                raise LLMError("schema_validation", "DeepSeek output failed Pydantic validation") from None
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                if isinstance(exc, LLMError):
                    raise
                raise LLMError("invalid_response", "DeepSeek returned an invalid envelope") from None
            metadata.update(validated_output=True, schema_valid=True)
            if tool_name:
                metadata["tool_call_valid"] = True
            return result
        except httpx.TimeoutException:
            metadata["error_type"] = "timeout"
            raise LLMError("timeout", "DeepSeek request timed out; no retry") from None
        except httpx.RequestError:
            metadata["error_type"] = "transport"
            raise LLMError("transport", "DeepSeek transport failure; no retry") from None
        except LLMError as exc:
            metadata["error_type"] = exc.code
            raise
        finally:
            metadata["latency_ms"] = round((perf_counter() - started) * 1000)
            self._metadata.set(metadata)

    async def _post(self, client: httpx.AsyncClient, payload: dict) -> httpx.Response:
        return await client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload, timeout=self.timeout, follow_redirects=False,
        )
