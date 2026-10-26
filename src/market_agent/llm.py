"""Repository-owned OpenAI-compatible structured completion contract."""

import asyncio
import json
import hashlib
from time import perf_counter
from contextvars import ContextVar
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class LLMError(ValueError):
    """A sanitized, typed LLM boundary failure."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class LLMConfigurationError(LLMError):
    pass


LLM_ENV_NAMES = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")


def openai_compatible_client_from_environment(
    environment: Mapping[str, str],
) -> "OpenAICompatibleLLMClient | None":
    values = {name: environment.get(name, "").strip() for name in LLM_ENV_NAMES}
    configured = [name for name, value in values.items() if value]
    if not configured:
        return None
    if len(configured) != len(LLM_ENV_NAMES):
        raise LLMConfigurationError(
            "configuration",
            "LLM configuration is partial; LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL are all required",
        )
    return OpenAICompatibleLLMClient(
        base_url=values["LLM_BASE_URL"],
        api_key=values["LLM_API_KEY"],
        model=values["LLM_MODEL"],
    )


class StructuredLLMRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid", frozen=True)

    purpose: str
    response_model: type[BaseModel]
    facts: dict
    evidence_ids: list[str]
    numeric_fact_tokens: list[str]
    grounded_responses: list[dict] = []
    generation_mode: Literal["select", "generate"] = "select"
    system_instruction: str | None = None
    verbosity: Literal["low", "medium", "high"] | None = None
    reasoning_effort: Literal["low", "medium"] = "medium"
    max_output_tokens: int = Field(default=8192, ge=256, le=8192)


class LLMClient(Protocol):
    model_name: str

    async def complete_structured(self, request: StructuredLLMRequest) -> BaseModel: ...


class OpenAICompatibleLLMClient:
    """Call the repository's narrow Chat Completions contract using httpx."""

    provider = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, model: str, *,
                 client: httpx.AsyncClient | None = None, timeout: float = 120.0, transport_factory=None, _offline_test=False):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.model_name = model
        self.client = client
        self.timeout = timeout
        self.transport_factory = transport_factory
        self._offline_test = _offline_test

    async def complete_structured(self, request: StructuredLLMRequest) -> BaseModel:
        prompt = {
            "purpose": request.purpose,
            "facts": request.facts,
            "allowed_evidence_ids": request.evidence_ids,
            "allowed_numeric_tokens": request.numeric_fact_tokens,
            "response_schema": request.response_model.model_json_schema(),
        }
        if request.generation_mode == "select":
            prompt["allowed_grounded_responses"] = request.grounded_responses
            system = (
                "Select and return exactly one JSON object from allowed_grounded_responses, "
                "unchanged. Each option already matches the response schema and grounded facts. "
                "Do not add, remove, paraphrase, calculate, convert, or introduce content."
            )
        else:
            if request.grounded_responses:
                prompt["response_shape_example"] = request.grounded_responses[0]
            system = request.system_instruction or (
                "Return exactly one JSON object matching response_schema. Use only supplied facts, "
                "cite only allowed_evidence_ids, and do not introduce unsupported numeric facts."
            )
        payload = {
            "model": self.model_name,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": system,
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)},
            ],
        }
        if request.generation_mode == "generate":
            payload["response_format"] = {"type": "json_object"}
        try:
            if self.client is None:
                async with httpx.AsyncClient(follow_redirects=False,trust_env=False,
                        transport=self.transport_factory() if self.transport_factory else None) as client:
                    self._admit(request,client)
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json=payload,
                        timeout=self.timeout, follow_redirects=False,
                    )
            else:
                self._admit(request,self.client)
                response = await self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                    timeout=self.timeout, follow_redirects=False,
                )
        except httpx.TimeoutException:
            raise LLMError("timeout", "LLM request timed out") from None
        except httpx.RequestError:
            raise LLMError("transport", "LLM transport failure") from None

        if response.status_code in (401, 403):
            raise LLMError("authentication", f"LLM authentication failed ({response.status_code})")
        if response.status_code == 429:
            raise LLMError("rate_limit", "LLM rate limit exceeded (429)")
        if response.status_code >= 500:
            raise LLMError("provider_unavailable", f"LLM provider unavailable ({response.status_code})")
        if response.status_code != 200:
            raise LLMError("provider", f"LLM request failed ({response.status_code})")

        try:
            body = response.json()
        except ValueError:
            raise LLMError("malformed_response", "LLM returned malformed response JSON") from None
        try:
            content = body["choices"][0]["message"].get("content")
        except (AttributeError, KeyError, IndexError, TypeError):
            content = None
        if not isinstance(content, str) or not content.strip():
            raise LLMError("empty_content", "LLM response has no assistant content")
        try:
            json.loads(content)
        except (TypeError, ValueError):
            raise LLMError("malformed_json", "LLM assistant content is not valid JSON") from None
        try:
            return request.response_model.model_validate_json(content)
        except ValidationError as exc:
            issues = "; ".join(
                f"{'.'.join(str(part) for part in issue['loc'])}:{issue['type']}"
                for issue in exc.errors(include_url=False, include_context=False, include_input=False)
            )
            raise LLMError(
                "schema_validation",
                f"LLM assistant content failed schema validation ({issues[:500]})",
            ) from None

    def _admit(self, request, client):
        from .research_egress import admit_model_send
        admit_model_send('compatible',self.model_name,self.base_url+'/chat/completions',request.facts,
            purpose=request.purpose,client=client,offline_test=self._offline_test)


class UnconfiguredLLMClient:
    provider = "openai"
    model_name = "gpt-6.1-sol"
    configuration_error = "configuration"

    def __init__(self, *, provider="openai", model="gpt-6.1-sol", message="OPENAI_API_KEY is required"):
        self.provider = provider
        self.model_name = model
        self.message = message

    async def complete_structured(self, request: StructuredLLMRequest) -> BaseModel:
        raise LLMConfigurationError("configuration", self.message)


def production_llm_from_environment(environment: Mapping[str, str]) -> LLMClient:
    """An alternate provider requires an explicit opt-in, never automatic failover."""
    provider = environment.get("LLM_PROVIDER", "openai").strip()
    if provider == "deepseek":
        from .deepseek import DEEPSEEK_MODELS, DeepSeekLLMClient

        model = environment.get("DEEPSEEK_MODEL", "").strip()
        if model not in DEEPSEEK_MODELS:
            raise LLMConfigurationError("configuration", "DEEPSEEK_MODEL must explicitly select an allowed model")
        key = environment.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            return UnconfiguredLLMClient(provider="deepseek", model=model,
                                        message="DEEPSEEK_API_KEY is required")
        from .runtime_budget import runtime_transport_factory
        return DeepSeekLLMClient(key, model,
            transport_factory=runtime_transport_factory(environment, 'DeepSeek'))
    if provider == "compatible":
        return openai_compatible_client_from_environment(environment) or UnconfiguredLLMClient(
            provider="openai_compatible", model="unconfigured-compatible",
            message="LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL are required")
    if provider != "openai":
        raise LLMConfigurationError("configuration", "LLM_PROVIDER must be openai, compatible or deepseek")
    key = environment.get("OPENAI_API_KEY", "").strip()
    if not key:
        return UnconfiguredLLMClient()
    from .runtime_budget import runtime_transport_factory
    return OpenAIResponsesLLMClient(key, model=environment.get("OPENAI_MODEL", "").strip() or "gpt-6.1-sol",
        max_transport_attempts=1, transport_factory=runtime_transport_factory(environment, 'OpenAI'))


def structured_output_schema(model: type[BaseModel]) -> dict:
    """Close the wire schema; canonical Pydantic/domain validation still runs afterwards."""
    schema = model.model_json_schema()
    # The neutral draft accepts invalid scores to classify abstention. The native
    # API requires closed keys, while the domain validator retains that responsibility.
    if model.__name__ in {"JudgeDraft", "OwnerJudgeDraft", "PriceJudgeDraft", "ContextJudgeDraft"}:
        schema["$defs"]["JudgeDraftAssessment"]["properties"]["scores"] = {
            "anyOf": [{"type": "object", "properties": {
                action: {"type": "integer", "minimum": 0, "maximum": 4}
                for action in ("ADD", "HOLD", "REDUCE", "EXIT")},
                "required": ["ADD", "HOLD", "REDUCE", "EXIT"], "additionalProperties": False},
                {"type": "null"}]}

    def close(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object":
                if "properties" not in node:
                    raise LLMConfigurationError("configuration", "Structured output requires closed object fields")
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                close(value)
        elif isinstance(node, list):
            for value in node:
                close(value)
    close(schema)
    return schema


def model_event_metadata(client, purpose: str) -> dict:
    metadata = getattr(client, "call_metadata", lambda: {})()
    return {"provider": getattr(client, "provider", None),
            "prompt_contract": (purpose.split(":", 1)[0] if purpose.startswith("tdb-decision-judge") else
                                "thesis_impact_semantic_assessment-v2" if purpose == "thesis_impact_semantic_assessment"
                                else f"{purpose}-v1"),
            "latency_ms": metadata.get("latency_ms"), "usage": metadata.get("usage", {}),
            "context_metrics": metadata.get("context_metrics", {}),
            "transport_attempts": metadata.get("transport_attempts", []),
            "validation_attempts": metadata.get("validation_attempts", [])}


def observed_model_event(client, **fields):
    from .domain import SynthesisEvent

    metadata = model_event_metadata(client, fields["purpose"])
    actual = getattr(client,'call_metadata',lambda:{})().get('actual_model')
    if fields['model'] != 'deterministic_python' and isinstance(actual,str) and 0 < len(actual) <= 128:
        fields['model'] = actual
    if fields["model"] == "deterministic_python":
        metadata = {"provider": "deterministic_python",
                    "prompt_contract": f"{fields['purpose']}-v1"}
    if metadata.get("latency_ms") is None:
        metadata["latency_ms"] = max(0, round((fields["ended_at"] - fields["started_at"]).total_seconds() * 1000))
    return SynthesisEvent(**{**metadata, "attempt": "grounded_retry" if fields["purpose"].endswith("_grounded_retry") else "initial",
        "validation_outcome": "rejected" if fields.get("status") == "error" else "accepted", **fields})


class OpenAIResponsesLLMClient:
    """Official Responses transport. No stored conversation or reasoning output."""
    provider = "openai"

    def __init__(self, api_key: str, model: str = "gpt-6.1-sol", *,
                 client: httpx.AsyncClient | None = None, timeout: float | None = None,
                 max_transport_attempts: int = 2, transport_factory=None, _offline_test=False):
        self._api_key = api_key
        self.model_name = model
        if max_transport_attempts not in {1,2}:
            raise ValueError('Transport attempts must stay bounded')
        self.max_transport_attempts = max_transport_attempts
        self.client = client
        if client is not None and transport_factory is not None:
            raise ValueError('Choose one explicit client or guarded transport factory')
        self.transport_factory = transport_factory
        self._offline_test = _offline_test
        self.timeout = 60.0 if timeout is None else timeout
        # Non-streamed holding judgments can legitimately take over a minute.
        # Keep the general budget unchanged; explicit caller budgets still win.
        self.decision_timeout = 180.0 if timeout is None else timeout
        self._metadata: ContextVar[dict] = ContextVar("openai_call_metadata", default={})

    def call_metadata(self) -> dict:
        return self._metadata.get()

    async def complete_structured(self, request: StructuredLLMRequest) -> BaseModel:
        self._metadata.set({})
        prepared = perf_counter()
        prompt = {"purpose": request.purpose, "facts": request.facts,
                  "allowed_evidence_ids": request.evidence_ids,
                  "allowed_numeric_tokens": request.numeric_fact_tokens}
        if request.generation_mode == "select":
            prompt["allowed_grounded_responses"] = request.grounded_responses
            system = ("Select and return exactly one JSON object from allowed_grounded_responses, "
                      "unchanged. Do not add, remove, paraphrase, calculate, or introduce content.")
        else:
            system = request.system_instruction or "Return grounded JSON using only supplied facts and evidence IDs."
            if request.grounded_responses:
                prompt["response_shape_example"] = request.grounded_responses[0]
        payload = {"model": self.model_name, "store": False, "reasoning": {"effort": request.reasoning_effort},
                   "max_output_tokens": request.max_output_tokens,
                   "input": [{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)}],
                   "text": {"format": {"type": "json_schema", "name": request.response_model.__name__,
                                       "strict": True, "schema": structured_output_schema(request.response_model)}}}
        if request.verbosity is not None:
            payload["text"]["verbosity"] = request.verbosity
        metrics = {"facts_bytes": len(json.dumps(request.facts, ensure_ascii=False, default=str).encode()),
                   "system_bytes": len(system.encode()),
                   "schema_bytes": len(json.dumps(payload["text"]["format"]["schema"]).encode()),
                   "projection_hash": hashlib.sha256(json.dumps(request.facts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest(),
                   "prepare_ms": round((perf_counter()-prepared)*1000)}
        self._metadata.set({"context_metrics": metrics})
        if request.facts.get("investment_context"):
            metrics.update(projection_version=request.facts.get("context_projection","idc-typed-v1"),
                selected_evidence_count=len(request.evidence_ids),
                packet_hash=request.facts["investment_context"]["packet_hash"])
        if self.client is None:
            async with httpx.AsyncClient(transport=self.transport_factory() if self.transport_factory else None,
                                         trust_env=False, follow_redirects=False) as client:
                return await self._complete(client, request, payload)
        return await self._complete(self.client, request, payload)

    async def _complete(self, client, request, payload):
        attempts = []
        usage = {}
        if request.purpose.startswith(('tdb-decision-judge-', 'cockpit-fast-decision-')):
            timeout = httpx.Timeout(self.decision_timeout,
                connect=min(10.0, self.decision_timeout),
                write=min(30.0, self.decision_timeout),
                pool=min(10.0, self.decision_timeout))
        else:
            timeout = httpx.Timeout(self.timeout)
        actual_model = self.model_name
        for attempt in range(1,self.max_transport_attempts+1):
            started = datetime.now(timezone.utc)
            error = None
            response = None
            timeout_phase = None
            try:
                from .research_egress import admit_model_send
                admit_model_send(self.provider,self.model_name,"https://api.openai.com/v1/responses",request.facts,
                    purpose=request.purpose,client=client,offline_test=self._offline_test)
                response = await client.post("https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {self._api_key}"}, json=payload, timeout=timeout, follow_redirects=False)
                status = response.status_code
                if status != 200:
                    code = ("authentication" if status in (401, 403) else "rate_limit" if status == 429
                            else "provider_unavailable" if status >= 500 else "provider")
                    raise LLMError(code, f"OpenAI HTTP {status}")
                try:
                    body = response.json()
                except ValueError:
                    raise LLMError("malformed_response", "OpenAI envelope is not JSON") from None
                if not isinstance(body, dict):
                    raise LLMError("malformed_response", "OpenAI envelope is not an object")
                if isinstance(body.get('model'),str):
                    actual_model = body['model']
                raw_usage = body.get("usage")
                if isinstance(raw_usage, dict):
                    usage = {key: raw_usage[key] for key in ("input_tokens", "output_tokens")
                             if type(raw_usage.get(key)) is int and raw_usage[key] >= 0}
                    details = raw_usage.get("input_tokens_details")
                    if isinstance(details, dict) and type(details.get("cached_tokens")) is int and details["cached_tokens"] >= 0:
                        usage["cached_input_tokens"] = details["cached_tokens"]
                    output_details = raw_usage.get("output_tokens_details")
                    if isinstance(output_details, dict) and type(output_details.get("reasoning_tokens")) is int and output_details["reasoning_tokens"] >= 0:
                        usage["reasoning_output_tokens"] = output_details["reasoning_tokens"]
                if body.get("status") == "incomplete":
                    raise LLMError("incomplete_response", "OpenAI output did not complete")
                output = body.get("output")
                if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
                    raise LLMError("malformed_response", "OpenAI output envelope is invalid")
                texts = []
                for item in output:
                    if item.get("type") != "message":
                        continue
                    content = item.get("content")
                    if not isinstance(content, list) or any(not isinstance(part, dict) for part in content):
                        raise LLMError("malformed_response", "OpenAI message envelope is invalid")
                    if any(part.get("type") == "refusal" for part in content):
                        raise LLMError("refusal", "OpenAI declined the structured task")
                    texts.extend(part.get("text") for part in content if part.get("type") == "output_text")
                if len(texts) != 1 or not isinstance(texts[0], str) or not texts[0].strip():
                    raise LLMError("empty_content", "OpenAI returned no single structured output")
                try:
                    json.loads(texts[0])
                except ValueError:
                    raise LLMError("malformed_json", "OpenAI output is not JSON") from None
                try:
                    return request.response_model.model_validate_json(texts[0])
                except ValidationError as exc:
                    # Provider-controlled locations/messages may contain private data.
                    # Only repository-owned top-level field names and Pydantic error
                    # types are useful for a bounded repair; never include input/message.
                    fields = request.response_model.model_fields
                    issues = sorted({f"{e['loc'][0]}:{e['type']}" for e in exc.errors()
                                     if e['loc'] and e['loc'][0] in fields})
                    raise LLMError("schema_validation", "OpenAI output failed typed validation " +
                                   ",".join(issues)[:220]) from None
            except asyncio.CancelledError:
                # Wall deadlines/shutdown cancel the request before HTTP returns.
                # Finally must not turn that interruption into a successful call.
                error = LLMError('cancelled', 'OpenAI request cancelled')
                raise
            except httpx.TimeoutException as exc:
                timeout_phase = next((phase for kind, phase in (
                    (httpx.ConnectTimeout, 'connect'), (httpx.ReadTimeout, 'read'),
                    (httpx.WriteTimeout, 'write'), (httpx.PoolTimeout, 'pool'))
                    if isinstance(exc, kind)), 'unknown')
                error = LLMError("timeout", "OpenAI request timed out")
            except httpx.RequestError:
                error = LLMError("transport", "OpenAI transport failure")
            except LLMError as exc:
                error = exc
            finally:
                ended = datetime.now(timezone.utc)
                attempts.append({"attempt": attempt, "started_at": started.isoformat(),
                    "ended_at": ended.isoformat(), "latency_ms": round((ended - started).total_seconds() * 1000),
                    "status": "error" if error else "success", "error": error.code if error else None,
                    "http_status": response.status_code if response is not None else None,
                    "timeout_phase": timeout_phase, "timeout_limits": timeout.as_dict()})
                self._metadata.set({"usage": usage, "transport_attempts": list(attempts),
                                    "actual_model":actual_model,
                                    "context_metrics": self._metadata.get().get("context_metrics", {}),
                                    "latency_ms": sum(row["latency_ms"] for row in attempts)})
            if attempt == self.max_transport_attempts or error.code not in {"transport", "provider_unavailable"}:
                raise error from None
        raise AssertionError("unreachable transport retry")
