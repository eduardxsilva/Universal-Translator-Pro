"""Provedor Microsoft Azure AI Translator v3.0."""
from __future__ import annotations

import time
from typing import Callable

import httpx

from ...domain.contracts import TranslationProviderPort
from ...domain.entities import TranslationRequest, TranslationResult
from ...domain.enums import ProviderType
from .resilience import CostEstimator, TranslationProviderError


class MicrosoftTranslatorProvider(TranslationProviderPort):
    """Integração REST com Microsoft Translator API v3.0."""

    ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate"

    def __init__(
        self,
        api_key_resolver: Callable[[], str],
        region_resolver: Callable[[], str] | None = None,
        timeout_seconds: float = 15.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._get_api_key = api_key_resolver
        self._get_region = region_resolver or (lambda: "brazilsouth")
        self._timeout = timeout_seconds
        self._client = http_client

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.MICROSOFT

    @property
    def display_name(self) -> str:
        return "Microsoft Azure Translator"

    def is_configured(self) -> bool:
        return bool((self._get_api_key() or "").strip())

    @staticmethod
    def _normalize_lang(code: str | None) -> str:
        if not code:
            return "en"
        c = code.strip().lower()
        if c == "pt-br":
            return "pt"
        if c in ("en-us", "en-gb"):
            return "en"
        if c == "zh":
            return "zh-Hans"
        return c.split("-")[0]

    def translate(self, request: TranslationRequest) -> TranslationResult:
        api_key = (self._get_api_key() or "").strip()
        if not api_key:
            raise TranslationProviderError(
                "Configure sua chave do Microsoft Translator.",
                provider=ProviderType.MICROSOFT,
                auth_failed=True,
                retryable=False,
            )

        text = (request.text or "").strip()
        if not text:
            raise TranslationProviderError(
                "Não há texto para traduzir.",
                provider=ProviderType.MICROSOFT,
                retryable=False,
            )

        target = self._normalize_lang(request.target_lang)
        params: dict[str, str] = {"api-version": "3.0", "to": target}
        if request.source_lang and request.source_lang.upper() not in ("ANY", "AUTO"):
            src = self._normalize_lang(request.source_lang)
            if src != target:
                params["from"] = src

        headers = {
            "Ocp-Apim-Subscription-Key": api_key,
            "Ocp-Apim-Subscription-Region": (self._get_region() or "brazilsouth").strip(),
            "Content-Type": "application/json",
        }
        body = [{"Text": text}]

        started = time.perf_counter()
        try:
            if self._client is not None:
                response = self._client.post(
                    self.ENDPOINT, params=params, headers=headers, json=body, timeout=self._timeout
                )
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(
                        self.ENDPOINT, params=params, headers=headers, json=body
                    )
        except Exception as exc:
            raise TranslationProviderError(
                f"Falha de conexão com Microsoft Translator: {exc}",
                provider=ProviderType.MICROSOFT,
                retryable=True,
            ) from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        status = response.status_code
        if status in (401, 403):
            raise TranslationProviderError(
                "Chave ou região do Microsoft Translator inválida (HTTP 401/403).",
                provider=ProviderType.MICROSOFT,
                status_code=status,
                auth_failed=True,
                retryable=False,
            )
        if status == 429 or status >= 500:
            raise TranslationProviderError(
                f"Microsoft Translator indisponível ou limite atingido (HTTP {status}).",
                provider=ProviderType.MICROSOFT,
                status_code=status,
                retryable=True,
            )
        if status >= 400:
            raise TranslationProviderError(
                f"Erro Microsoft Translator (HTTP {status}): {(response.text or '')[:300]}",
                provider=ProviderType.MICROSOFT,
                status_code=status,
                retryable=False,
            )

        try:
            data = response.json()
            item = data[0]
            translated = str(item["translations"][0]["text"]).strip()
            detected = str(
                item.get("detectedLanguage", {}).get("language")
                or request.source_lang
                or "PT"
            ).upper()
        except Exception as exc:
            raise TranslationProviderError(
                "Resposta inválida do Microsoft Translator.",
                provider=ProviderType.MICROSOFT,
                retryable=False,
            ) from exc

        chars = len(text)
        return TranslationResult(
            translated_text=translated,
            source_text=text,
            detected_source_lang=detected,
            target_lang=request.target_lang,
            provider=ProviderType.MICROSOFT,
            cached=False,
            latency_ms=round(latency_ms, 2),
            characters_billed=chars,
            estimated_cost_usd=CostEstimator.estimate_usd(ProviderType.MICROSOFT, chars),
            direction=request.direction,
            app_profile_name=request.app_profile_name,
            operation_id=request.operation_id,
        )
