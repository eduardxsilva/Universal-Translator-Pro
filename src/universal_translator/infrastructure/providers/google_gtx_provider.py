"""Provedor neural gratuito sem necessidade de chave de API (Google GTX Web Endpoint) para funcionamento imediato."""
from __future__ import annotations

import time
import urllib.parse

import httpx

from ...domain.contracts import TranslationProviderPort
from ...domain.entities import TranslationRequest, TranslationResult
from ...domain.enums import ProviderType
from .resilience import TranslationProviderError


class GoogleGTXFreeProvider(TranslationProviderPort):
    """Tradutor neural automático (endpoint GTX) que funciona sem configuração de API key.

    Garante que o usuário consiga traduzir e substituir qualquer frase imediatamente
    mesmo antes de cadastrar chaves pagas da DeepL, Google Cloud, Microsoft ou OpenAI.
    """

    ENDPOINT = "https://translate.googleapis.com/translate_a/single"

    def __init__(
        self,
        timeout_seconds: float = 12.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._client = http_client

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.GOOGLE

    @property
    def display_name(self) -> str:
        return "Google Translate Automático (Sem Chave)"

    def is_configured(self) -> bool:
        return True

    @staticmethod
    def _norm_lang(code: str | None, default: str = "auto") -> str:
        if not code:
            return default
        upper = code.strip().upper()
        if upper in ("ANY", "AUTO", ""):
            return "auto"
        if upper in ("PT-BR", "PT"):
            return "pt"
        if upper in ("EN-US", "EN-GB", "EN"):
            return "en"
        if upper in ("ZH", "ZH-CN"):
            return "zh-CN"
        return upper.split("-")[0].lower()

    def translate(self, request: TranslationRequest) -> TranslationResult:
        text = (request.text or "").strip()
        if not text:
            raise TranslationProviderError(
                "Não há texto para traduzir.",
                provider=ProviderType.GOOGLE,
                retryable=False,
            )

        sl = self._norm_lang(request.source_lang, default="auto")
        tl = self._norm_lang(request.target_lang, default="en")
        if sl == tl and sl != "auto":
            sl = "auto"

        params = {
            "client": "gtx",
            "sl": sl,
            "tl": tl,
            "dt": "t",
            "q": text,
        }

        started = time.perf_counter()
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            }
            if self._client is not None:
                response = self._client.get(
                    self.ENDPOINT, params=params, headers=headers, timeout=self._timeout
                )
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.get(self.ENDPOINT, params=params, headers=headers)
        except Exception as exc:
            raise TranslationProviderError(
                f"Falha de conexão no tradutor automático: {exc}",
                provider=ProviderType.GOOGLE,
                retryable=True,
            ) from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        if response.status_code >= 400:
            raise TranslationProviderError(
                f"Erro HTTP {response.status_code} no tradutor automático.",
                provider=ProviderType.GOOGLE,
                status_code=response.status_code,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )

        try:
            data = response.json()
            # Formato GTX: [[[translated_chunk, original_chunk, ...], ...], null, detected_lang]
            segments = data[0]
            translated_parts = [str(seg[0]) for seg in segments if seg and seg[0] is not None]
            translated_text = "".join(translated_parts).strip()
            detected_lang = str(data[2] if len(data) > 2 and data[2] else sl).upper()
        except Exception as exc:
            raise TranslationProviderError(
                "Resposta inválida do tradutor automático.",
                provider=ProviderType.GOOGLE,
                retryable=False,
            ) from exc

        if not translated_text:
            raise TranslationProviderError(
                "Tradução retornou vazia.",
                provider=ProviderType.GOOGLE,
                retryable=False,
            )

        return TranslationResult(
            translated_text=translated_text,
            source_text=text,
            detected_source_lang=detected_lang,
            target_lang=request.target_lang,
            provider=ProviderType.GOOGLE,
            cached=False,
            latency_ms=round(latency_ms, 2),
            characters_billed=0,
            estimated_cost_usd=0.0,
            direction=request.direction,
            app_profile_name=request.app_profile_name,
            operation_id=request.operation_id,
        )
