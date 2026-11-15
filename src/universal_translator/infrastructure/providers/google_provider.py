"""Provedor Google Cloud Translation API v2."""
from __future__ import annotations

import html
import time
from typing import Callable

import httpx

from ...domain.contracts import TranslationProviderPort
from ...domain.entities import TranslationRequest, TranslationResult
from ...domain.enums import ProviderType
from .resilience import CostEstimator, TranslationProviderError


class GoogleTranslateProvider(TranslationProviderPort):
    """Integração REST com Google Cloud Translation API v2."""

    ENDPOINT = "https://translation.googleapis.com/language/translate/v2"

    def __init__(
        self,
        api_key_resolver: Callable[[], str],
        timeout_seconds: float = 15.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._get_api_key = api_key_resolver
        self._timeout = timeout_seconds
        self._client = http_client

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.GOOGLE

    @property
    def display_name(self) -> str:
        return "Google Cloud Translation"

    def is_configured(self) -> bool:
        return bool((self._get_api_key() or "").strip())

    @staticmethod
    def _normalize_lang(code: str | None) -> str:
        if not code:
            return "en"
        upper = code.strip().upper()
        if upper in ("ZH", "ZH-CN"):
            return "zh-CN"
        return upper.split("-")[0].lower()

    def translate(self, request: TranslationRequest) -> TranslationResult:
        api_key = (self._get_api_key() or "").strip()
        if not api_key:
            raise TranslationProviderError(
                "Configure sua chave da API Google Cloud Translation.",
                provider=ProviderType.GOOGLE,
                auth_failed=True,
                retryable=False,
            )

        text = (request.text or "").strip()
        if not text:
            raise TranslationProviderError(
                "Não há texto para traduzir.",
                provider=ProviderType.GOOGLE,
                retryable=False,
            )

        target = self._normalize_lang(request.target_lang)
        payload: dict[str, str] = {
            "q": text,
            "target": target,
            "format": "text",
        }
        if request.source_lang and request.source_lang.upper() not in ("ANY", "AUTO"):
            src = self._normalize_lang(request.source_lang)
            if src != target:
                payload["source"] = src

        started = time.perf_counter()
        try:
            params = {"key": api_key}
            if self._client is not None:
                response = self._client.post(
                    self.ENDPOINT, params=params, json=payload, timeout=self._timeout
                )
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(self.ENDPOINT, params=params, json=payload)
        except Exception as exc:
            raise TranslationProviderError(
                f"Falha de conexão com Google Translation: {exc}",
                provider=ProviderType.GOOGLE,
                retryable=True,
            ) from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        status = response.status_code
        if status in (401, 403):
            raise TranslationProviderError(
                "Chave Google Cloud Translation inválida ou sem permissão (HTTP 403).",
                provider=ProviderType.GOOGLE,
                status_code=status,
                auth_failed=True,
                retryable=False,
            )
        if status == 429 or status >= 500:
            raise TranslationProviderError(
                f"Google Cloud Translation temporariamente indisponível (HTTP {status}).",
                provider=ProviderType.GOOGLE,
                status_code=status,
                retryable=True,
            )
        if status >= 400:
            raise TranslationProviderError(
                f"Erro Google Translation (HTTP {status}): {(response.text or '')[:300]}",
                provider=ProviderType.GOOGLE,
                status_code=status,
                retryable=False,
            )

        try:
            data = response.json()
            item = data["data"]["translations"][0]
            translated = html.unescape(str(item["translatedText"])).strip()
            detected = str(item.get("detectedSourceLanguage") or request.source_lang or "PT").upper()
        except Exception as exc:
            raise TranslationProviderError(
                "Resposta inválida do Google Cloud Translation.",
                provider=ProviderType.GOOGLE,
                retryable=False,
            ) from exc

        chars = len(text)
        return TranslationResult(
            translated_text=translated,
            source_text=text,
            detected_source_lang=detected,
            target_lang=request.target_lang,
            provider=ProviderType.GOOGLE,
            cached=False,
            latency_ms=round(latency_ms, 2),
            characters_billed=chars,
            estimated_cost_usd=CostEstimator.estimate_usd(ProviderType.GOOGLE, chars),
            direction=request.direction,
            app_profile_name=request.app_profile_name,
            operation_id=request.operation_id,
        )
