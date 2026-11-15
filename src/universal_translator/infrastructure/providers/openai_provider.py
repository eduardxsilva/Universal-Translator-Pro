"""Provedor OpenAI (GPT-4o-mini / compatível) com suporte a tradução contextual, gírias e formalidade."""
from __future__ import annotations

import time
from typing import Callable

import httpx

from ...domain.contracts import TranslationProviderPort
from ...domain.entities import TranslationRequest, TranslationResult
from ...domain.enums import ProviderType
from .resilience import CostEstimator, TranslationProviderError


class OpenAIProvider(TranslationProviderPort):
    """Integração com OpenAI API para tradução contextual sensível a registro e expressões idiomáticas."""

    ENDPOINT = "https://api.openai.com/v1/chat/completions"

    def __init__(
        self,
        api_key_resolver: Callable[[], str],
        model_resolver: Callable[[], str] | None = None,
        timeout_seconds: float = 20.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._get_api_key = api_key_resolver
        self._get_model = model_resolver or (lambda: "gpt-4o-mini")
        self._timeout = timeout_seconds
        self._client = http_client

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.OPENAI

    @property
    def display_name(self) -> str:
        return "OpenAI Contextual Translator"

    def is_configured(self) -> bool:
        return bool((self._get_api_key() or "").strip())

    def translate(self, request: TranslationRequest) -> TranslationResult:
        api_key = (self._get_api_key() or "").strip()
        if not api_key:
            raise TranslationProviderError(
                "Configure sua chave da API OpenAI.",
                provider=ProviderType.OPENAI,
                auth_failed=True,
                retryable=False,
            )

        text = (request.text or "").strip()
        if not text:
            raise TranslationProviderError(
                "Não há texto para traduzir.",
                provider=ProviderType.OPENAI,
                retryable=False,
            )

        system_lines = [
            "You are a precision multilingual translation engine.",
            f"Translate the user's input strictly into target language '{request.target_lang}'.",
            f"Formality / register preference: {request.formality.value}.",
            "Preserve any token formatted like __UTGLOSS0__ exactly as written.",
            "Do NOT invent context, do NOT add explanations, and output ONLY the translated text.",
        ]
        if request.context_turns:
            history_str = "\n".join(
                f"- [{t.source_lang}->{t.target_lang}] {t.source_text} => {t.translated_text}"
                for t in request.context_turns[-3:]
            )
            system_lines.append(
                f"Recent conversation turns for disambiguation only (do not translate these again):\n{history_str}"
            )

        payload = {
            "model": (self._get_model() or "gpt-4o-mini").strip(),
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": "\n".join(system_lines)},
                {"role": "user", "content": text},
            ],
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        started = time.perf_counter()
        try:
            if self._client is not None:
                response = self._client.post(
                    self.ENDPOINT, headers=headers, json=payload, timeout=self._timeout
                )
            else:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(self.ENDPOINT, headers=headers, json=payload)
        except Exception as exc:
            # Em chamadas LLM pagas, não repetimos cegamente em caso de timeout de leitura para evitar cobrança dupla
            raise TranslationProviderError(
                f"Falha de conexão com OpenAI: {exc}",
                provider=ProviderType.OPENAI,
                retryable=False,
            ) from exc

        latency_ms = (time.perf_counter() - started) * 1000.0
        status = response.status_code
        if status in (401, 403):
            raise TranslationProviderError(
                "Chave da API OpenAI inválida ou sem permissão (HTTP 401/403).",
                provider=ProviderType.OPENAI,
                status_code=status,
                auth_failed=True,
                retryable=False,
            )
        if status == 429:
            raise TranslationProviderError(
                "Limite de requisições/cota OpenAI atingido (HTTP 429).",
                provider=ProviderType.OPENAI,
                status_code=status,
                quota_exceeded=True,
                retryable=False,
            )
        if status >= 400:
            raise TranslationProviderError(
                f"Erro OpenAI (HTTP {status}): {(response.text or '')[:300]}",
                provider=ProviderType.OPENAI,
                status_code=status,
                retryable=False,
            )

        try:
            data = response.json()
            translated = str(data["choices"][0]["message"]["content"]).strip()
        except Exception as exc:
            raise TranslationProviderError(
                "Resposta inválida da API OpenAI.",
                provider=ProviderType.OPENAI,
                retryable=False,
            ) from exc

        chars = len(text)
        return TranslationResult(
            translated_text=translated,
            source_text=text,
            detected_source_lang=request.source_lang or "AUTO",
            target_lang=request.target_lang,
            provider=ProviderType.OPENAI,
            cached=False,
            latency_ms=round(latency_ms, 2),
            characters_billed=chars,
            estimated_cost_usd=CostEstimator.estimate_usd(ProviderType.OPENAI, chars),
            direction=request.direction,
            app_profile_name=request.app_profile_name,
            operation_id=request.operation_id,
        )
