"""Cliente HTTP hacia el modelo upstream (OpenAI-compatible).

Añade sobre el reenvío simple: reintentos con backoff exponencial y jitter
para errores temporales (HTTP 429/5xx, timeouts, desconexiones, SSE
incompleto), respeto del header ``Retry-After``, proveedores distintos según la
fase (planificador / ejecutor / síntesis / verificación) y captura de métricas
de uso y reintentos.

Los reintentos de streaming solo ocurren si todavía no se emitió nada hacia el
cliente: repetir a mitad de stream duplicaría texto ya entregado.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import random
from collections.abc import AsyncIterator
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional

import httpx

from .budget import ExecutionBudget
from .config import settings
from .observability import RunMetrics, Usage, log
from .runtime import PhaseConfig

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class UpstreamError(Exception):
    """Error del upstream. Puede ser reintentable o definitivo."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retryable: bool = False,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


def _parse_retry_after(response: httpx.Response) -> Optional[float]:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (target - now).total_seconds())


class UpstreamClient:
    def __init__(
        self,
        metrics: Optional[RunMetrics] = None,
        budget: Optional[ExecutionBudget] = None,
        providers: Optional[Dict[str, PhaseConfig]] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._metrics = metrics
        self._budget = budget
        self._providers: Dict[str, PhaseConfig] = dict(providers or {})
        self._timeout = timeout

    def set_providers(self, providers: Dict[str, PhaseConfig]) -> None:
        self._providers = dict(providers)

    def provider(self, phase: str, model: str = "") -> PhaseConfig:
        config = self._providers.get(phase)
        if config is None:
            config = PhaseConfig(
                model=model or settings.upstream_model,
                base_url=settings.upstream_base_url.rstrip("/"),
                api_key=settings.resolved_api_key(),
            )
        elif not config.base_url or not config.api_key:
            # La config por fase puede traer solo el modelo: el resto hereda
            # del upstream global para no perder credenciales ni endpoint.
            config = PhaseConfig(
                model=config.model,
                base_url=config.base_url or settings.upstream_base_url.rstrip("/"),
                api_key=config.api_key or settings.resolved_api_key(),
                temperature=config.temperature,
            )
        if model and model != config.model:
            return PhaseConfig(
                model=model,
                base_url=config.base_url,
                api_key=config.api_key,
                temperature=config.temperature,
            )
        return config

    def _timeout_value(self) -> float:
        return float(self._timeout or settings.request_timeout_seconds)

    # ------------------------------------------------------------------
    # Reintentos y construcción de payload
    # ------------------------------------------------------------------

    def compute_delay(self, attempt: int, retry_after: Optional[float] = None) -> float:
        """Backoff exponencial con jitter, respetando ``Retry-After``."""
        base = max(0.0, float(settings.upstream_retry_base_seconds))
        cap = max(base, float(settings.upstream_retry_max_seconds))
        delay = min(cap, base * (2 ** max(0, attempt)))
        delay *= random.uniform(0.5, 1.5)
        if retry_after is not None:
            ceiling = max(0.0, float(settings.upstream_max_retry_after_seconds))
            delay = max(delay, min(retry_after, ceiling))
        return max(0.0, delay)

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        if isinstance(exc, UpstreamError):
            return bool(exc.retryable or exc.status_code in RETRYABLE_STATUS)
        if isinstance(exc, httpx.TransportError):
            # Incluye timeouts y desconexiones durante el streaming.
            return True
        if isinstance(exc, (json.JSONDecodeError, ValueError)):
            return True  # respuesta ilegible o incompleta
        return False

    def _as_upstream_error(self, exc: BaseException) -> UpstreamError:
        if isinstance(exc, UpstreamError):
            return exc
        return UpstreamError(
            f"Error de red con el upstream ({type(exc).__name__}): {exc}", retryable=True
        )

    def _record_call(self, phase: str, model: str) -> None:
        if self._budget is not None:
            self._budget.register_call()
        if self._metrics is not None:
            self._metrics.record_call(phase, model)

    def _record_retry(self, phase: str, reason: str, delay: float) -> None:
        if self._metrics is not None:
            self._metrics.record_retry(phase, reason)
        log(
            "upstream_retry",
            level=30,
            phase=phase,
            reason=reason[:300],
            delay_seconds=round(delay, 3),
            request_id=self._metrics.request_id if self._metrics else None,
        )

    def _record_usage(self, phase: str, model: str, payload: Any) -> None:
        usage = Usage.from_upstream(payload)
        if usage and self._metrics is not None:
            self._metrics.record_usage(phase, model, usage)

    async def _sleep(self, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)

    @staticmethod
    def _headers(api_key: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        phase: str,
        stream: bool,
        json_mode: bool,
        tools: Optional[list[dict[str, Any]]],
        tool_choice: Optional[Any],
        params: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        provider = self.provider(phase, model)
        payload: Dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "stream": stream,
        }
        for key, value in (params or {}).items():
            if value is not None:
                payload[key] = value
        # El contrato interno manda sobre los parámetros del cliente.
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if stream:
            # Sin esto el upstream no reporta tokens de las llamadas internas.
            payload["stream_options"] = {"include_usage": True}
        return payload


    @staticmethod
    def _http_error(response: httpx.Response, body: str) -> UpstreamError:
        status = response.status_code
        return UpstreamError(
            f"Upstream error {status}: {body}",
            status_code=status,
            retryable=status in RETRYABLE_STATUS,
            retry_after=_parse_retry_after(response),
        )

    async def _post_json(self, payload: Dict[str, Any], phase: str) -> Dict[str, Any]:
        provider = self.provider(phase, str(payload.get("model") or ""))
        response: httpx.Response
        self._record_call(phase, provider.model)
        async with httpx.AsyncClient(timeout=self._timeout_value()) as client:
            response = await client.post(
                f"{provider.base_url}/v1/chat/completions",
                headers=self._headers(provider.api_key),
                json=payload,
            )
        if response.status_code >= 400:
            raise self._http_error(response, response.text)
        data = response.json()
        self._record_usage(phase, provider.model, data.get("usage"))
        return data

    async def _call_with_retries(self, payload: Dict[str, Any], *, phase: str) -> Dict[str, Any]:
        attempts = max(0, int(settings.upstream_max_retries))
        for attempt in range(attempts + 1):
            try:
                return await self._post_json(payload, phase)
            except (UpstreamError, httpx.TransportError, json.JSONDecodeError, ValueError) as exc:
                if not self._is_retryable(exc) or attempt >= attempts:
                    raise self._as_upstream_error(exc)
                delay = self.compute_delay(attempt, getattr(exc, "retry_after", None))
                self._record_retry(phase, str(exc), delay)
                await self._sleep(delay)
        raise UpstreamError("No se pudo completar la llamada al upstream")  # pragma: no cover

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        phase: str = "planner",
        json_mode: bool = False,
        params: Optional[Dict[str, Any]] = None,
    ) -> str:
        message = await self.complete_raw(
            messages, model=model, phase=phase, json_mode=json_mode, params=params
        )
        return message.get("content") or ""

    async def complete_raw(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        phase: str = "planner",
        json_mode: bool = False,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = self._build_payload(
            messages=messages,
            model=model,
            phase=phase,
            stream=False,
            json_mode=json_mode,
            tools=tools,
            tool_choice=tool_choice,
            params=params,
        )
        data = await self._call_with_retries(payload, phase=phase)
        choices = data.get("choices") or []
        if not choices:
            raise UpstreamError("El upstream devolvió una respuesta sin 'choices'", retryable=True)
        choice = choices[0]
        message = choice.get("message") or {}
        message.setdefault("finish_reason", choice.get("finish_reason"))
        return message


    async def _stream_once(
        self, payload: Dict[str, Any], *, phase: str
    ) -> AsyncIterator[Dict[str, Any]]:
        provider = self.provider(phase, str(payload.get("model") or ""))
        self._record_call(phase, provider.model)
        saw_done = False
        finish_reason: Optional[str] = None
        async with httpx.AsyncClient(timeout=self._timeout_value()) as client:
            async with client.stream(
                "POST",
                f"{provider.base_url}/v1/chat/completions",
                headers=self._headers(provider.api_key),
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise self._http_error(response, body)

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        saw_done = True
                        break
                    if not data_str:
                        continue
                    chunk = json.loads(data_str)
                    self._record_usage(phase, provider.model, chunk.get("usage"))
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    yield choice

        if not saw_done and finish_reason is None:
            raise UpstreamError(
                "El stream del upstream terminó sin [DONE] ni finish_reason (SSE incompleto)",
                retryable=True,
            )

    async def stream_raw(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        phase: str = "executor",
        json_mode: bool = False,
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        payload = self._build_payload(
            messages=messages,
            model=model,
            phase=phase,
            stream=True,
            json_mode=json_mode,
            tools=tools,
            tool_choice=tool_choice,
            params=params,
        )
        attempts = max(0, int(settings.upstream_max_retries))
        attempt = 0
        while True:
            emitted = False
            try:
                async for choice in self._stream_once(payload, phase=phase):
                    emitted = True
                    yield {
                        "delta": choice.get("delta", {}),
                        "finish_reason": choice.get("finish_reason"),
                    }
                return
            except (UpstreamError, httpx.TransportError, json.JSONDecodeError, ValueError) as exc:
                # Repetir después de emitir texto duplicaría lo ya entregado.
                if emitted or not self._is_retryable(exc) or attempt >= attempts:
                    raise self._as_upstream_error(exc)
                delay = self.compute_delay(attempt, getattr(exc, "retry_after", None))
                self._record_retry(phase, str(exc), delay)
                attempt += 1
                await self._sleep(delay)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        phase: str = "executor",
        json_mode: bool = False,
        params: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[str]:
        async for chunk in self.stream_raw(
            messages, model=model, phase=phase, json_mode=json_mode, params=params
        ):
            piece = chunk["delta"].get("content")
            if piece:
                yield piece

