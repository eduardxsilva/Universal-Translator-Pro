"""Cliente REST oficial da DeepL API v2 com retries conservadores, suporte Free/Pro e consulta de uso."""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import httpx

from ...domain.contracts import TranslationProviderPort
from ...domain.entities import TranslationRequest, TranslationResult
from ...domain.enums import DeepLAccountType, FormalityLevel, ProviderType
from .resilience import CostEstimator, TranslationProviderError

_DEEPL_FORMALITY_LANGS = {
    "DE",
    "FR",
    "IT",
    "ES",
    "NL",
    "PL",
    "PT-BR",
    "PT-PT",
    "RU",
    "JA",
}


class DeepLProvider(TranslationProviderPort):
    """Integração oficial e exclusiva DeepL Free/Pro via API REST v2 (/v2/translate e /v2/usage).

    Nunca registra nem expõe a API Key em logs, exceções ou arquivos de configuração.
    """

    def __init__(
        self,
        api_key_resolver: Callable[[], str],
        timeout_seconds: float = 15.0,
        http_client: httpx.Client | None = None,
        max_retries: int = 2,
        sleep_fn: Callable[[float], None] = time.sleep,
        account_type_resolver: Callable[[], str] | None = None,
    ) -> None:
        self._get_api_key = api_key_resolver
        self._get_account_type = account_type_resolver or (lambda: DeepLAccountType.AUTO.value)
        self._timeout = max(3.0, float(timeout_seconds))
        self._client = http_client
        self._max_retries = max(0, min(int(max_retries), 4))
        self._sleep = sleep_fn

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.DEEPL

    @property
    def display_name(self) -> str:
        return "DeepL API (Free / Pro)"

    def is_configured(self) -> bool:
        return bool((self._get_api_key() or "").strip())

    def resolve_account_tier(self, key: str | None = None) -> str:
        """Determina se a conta é 'free' ou 'pro' conforme configuração explícita ou sufixo ':fx'."""
        configured = (self._get_account_type() or DeepLAccountType.AUTO.value).strip().lower()
        if configured == DeepLAccountType.FREE.value:
            return "free"
        if configured == DeepLAccountType.PRO.value:
            return "pro"
        resolved_key = (key if key is not None else (self._get_api_key() or "")).strip()
        return "free" if resolved_key.endswith(":fx") else "pro"

    def _credentials(self) -> tuple[str, str]:
        key = (self._get_api_key() or "").strip()
        if not key:
            raise TranslationProviderError(
                "Configure a chave da DeepL nas configurações.",
                provider=ProviderType.DEEPL,
                auth_failed=True,
                retryable=False,
            )
        tier = self.resolve_account_tier(key)
        base = "https://api-free.deepl.com" if tier == "free" else "https://api.deepl.com"
        return key, base

    @staticmethod
    def normalize_target_lang(lang: str) -> str:
        code = (lang or "EN-US").strip().upper()
        return {"EN": "EN-US", "PT": "PT-BR"}.get(code, code)

    @staticmethod
    def normalize_source_lang(lang: str | None) -> str | None:
        if not lang or lang.strip().upper() in ("AUTO", "ANY", ""):
            return None
        return lang.strip().upper().split("-")[0]

    @staticmethod
    def _retry_after(value: str | None, fallback: float) -> float:
        if not value:
            return fallback
        try:
            return max(0.0, min(30.0, float(value)))
        except ValueError:
            try:
                dt = parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, min(30.0, (dt - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                return fallback

    def _request(self, method: str, url: str, key: str, **kwargs) -> httpx.Response:
        headers = {
            "Authorization": f"DeepL-Auth-Key {key}",
            "User-Agent": "UniversalTranslatorPro/3.0",
        }
        if self._client is not None:
            client = self._client
            own = False
        else:
            client = httpx.Client(timeout=self._timeout)
            own = True
        try:
            for attempt in range(self._max_retries + 1):
                try:
                    response = (
                        client.get(url, headers=headers, timeout=self._timeout, **kwargs)
                        if method == "GET"
                        else client.post(url, headers=headers, timeout=self._timeout, **kwargs)
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                    if attempt >= self._max_retries:
                        raise TranslationProviderError(
                            "Falha ao conectar à DeepL (serviço indisponível ou sem conexão).",
                            provider=ProviderType.DEEPL,
                            retryable=False,
                        ) from exc
                    self._sleep(min(8.0, 0.4 * (2**attempt) + random.uniform(0.0, 0.25)))
                    continue
                if response.status_code == 429 and attempt < self._max_retries:
                    delay = self._retry_after(
                        response.headers.get("Retry-After") if hasattr(response, "headers") and response.headers else None,
                        min(8.0, 0.5 * (2**attempt) + random.uniform(0.0, 0.3)),
                    )
                    self._sleep(delay)
                    continue
                if response.status_code in (502, 503, 504) and method == "GET" and attempt < self._max_retries:
                    delay = self._retry_after(
                        response.headers.get("Retry-After") if hasattr(response, "headers") and response.headers else None,
                        min(6.0, 0.5 * (2**attempt) + random.uniform(0.0, 0.25)),
                    )
                    self._sleep(delay)
                    continue
                return response
            raise RuntimeError("DeepL request retry exhausted")
        finally:
            if own:
                client.close()

    @staticmethod
    def _raise_for_response(response: httpx.Response) -> None:
        status = response.status_code
        if status == 401:
            raise TranslationProviderError(
                "Não autorizado na DeepL (HTTP 401): verifique sua API Key.",
                provider=ProviderType.DEEPL,
                status_code=401,
                auth_failed=True,
                retryable=False,
            )
        if status == 403:
            raise TranslationProviderError(
                "Chave DeepL inválida ou assinatura inativa (HTTP 403).",
                provider=ProviderType.DEEPL,
                status_code=403,
                auth_failed=True,
                retryable=False,
            )
        if status == 456:
            raise TranslationProviderError(
                "Cota de caracteres da DeepL atingida (HTTP 456).",
                provider=ProviderType.DEEPL,
                status_code=456,
                quota_exceeded=True,
                retryable=False,
            )
        if status == 429:
            raise TranslationProviderError(
                "DeepL limitou temporariamente as requisições (HTTP 429).",
                provider=ProviderType.DEEPL,
                status_code=429,
                retryable=False,
            )
        if status >= 500:
            raise TranslationProviderError(
                f"Serviço DeepL temporariamente indisponível (HTTP {status}); operação cancelada com segurança.",
                provider=ProviderType.DEEPL,
                status_code=status,
                retryable=False,
            )
        if status >= 400:
            detail = (getattr(response, "text", "") or "")[:300]
            raise TranslationProviderError(
                f"Requisição DeepL inválida (HTTP {status}): {detail}",
                provider=ProviderType.DEEPL,
                status_code=status,
                retryable=False,
            )

    def test_connection(self) -> dict[str, int | str]:
        key, base = self._credentials()
        response = self._request("GET", f"{base}/v2/usage", key)
        self._raise_for_response(response)
        data = response.json()
        if not isinstance(data, dict):
            raise TranslationProviderError(
                "Resposta inválida do endpoint /v2/usage da DeepL.",
                provider=ProviderType.DEEPL,
            )
        char_count = int(data.get("character_count", 0))
        char_limit = int(data.get("character_limit", 0))
        return {
            "character_count": char_count,
            "character_limit": char_limit,
            "character_remaining": max(0, char_limit - char_count) if char_limit > 0 else 0,
            "account_tier": self.resolve_account_tier(key),
            "endpoint": base,
        }

    def get_usage(self) -> dict[str, int | str]:
        return self.test_connection()

    def translate(self, request: TranslationRequest) -> TranslationResult:
        key, base = self._credentials()
        text = request.text or ""
        if not text.strip():
            raise TranslationProviderError(
                "Não há texto para traduzir.",
                provider=ProviderType.DEEPL,
            )
        if len(text.encode("utf-8")) > 128 * 1024:
            raise TranslationProviderError(
                "Texto excede o limite de 128 KiB por solicitação DeepL.",
                provider=ProviderType.DEEPL,
            )
        target = self.normalize_target_lang(request.target_lang)
        source = self.normalize_source_lang(request.source_lang)
        payload: dict[str, object] = {
            "text": [text],
            "target_lang": target,
            "show_billed_characters": True,
        }
        if source and source != target.split("-")[0]:
            payload["source_lang"] = source
        if request.formality != FormalityLevel.DEFAULT and target in _DEEPL_FORMALITY_LANGS:
            payload["formality"] = (
                "prefer_more"
                if request.formality in (FormalityLevel.FORMAL, FormalityLevel.BUSINESS)
                else "prefer_less"
            )
        if request.context_turns:
            snippets = [
                f"{t.source_text} -> {t.translated_text}"
                for t in request.context_turns[-3:]
                if t.source_text.strip()
            ]
            if snippets:
                payload["context"] = " | ".join(snippets)[:1000]

        started = time.perf_counter()
        try:
            response = self._request("POST", f"{base}/v2/translate", key, json=payload)
            self._raise_for_response(response)
            data = response.json()
            if not isinstance(data, dict) or "translations" not in data or not data["translations"]:
                raise ValueError("Estrutura JSON sem campo translations")
            first = data["translations"][0]
            translated = str(first["text"])
            detected = str(first.get("detected_source_language") or source or "")
            billed = int(data.get("billed_characters", first.get("billed_characters", len(text))))
            confirmed_flag = ("billed_characters" in data) or ("billed_characters" in first)
        except TranslationProviderError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TranslationProviderError(
                "Falha de rede ou timeout na DeepL; solicitação não repetida para evitar cobrança duplicada.",
                provider=ProviderType.DEEPL,
                retryable=False,
            ) from exc
        except Exception as exc:
            raise TranslationProviderError(
                "Resposta inválida recebida da DeepL.",
                provider=ProviderType.DEEPL,
            ) from exc

        if not translated.strip():
            raise TranslationProviderError(
                "A DeepL retornou uma tradução vazia.",
                provider=ProviderType.DEEPL,
            )

        free = self.resolve_account_tier(key) == "free"
        return TranslationResult(
            translated_text=translated,
            source_text=text,
            detected_source_lang=detected or "AUTO",
            target_lang=target,
            provider=ProviderType.DEEPL,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            characters_billed=billed,
            estimated_cost_usd=CostEstimator.estimate_usd(
                ProviderType.DEEPL, billed, is_free_tier=free
            ),
            direction=request.direction,
            app_profile_name=request.app_profile_name,
            operation_id=request.operation_id,
            metadata={
                "billed_characters_confirmed": confirmed_flag,
                "account_tier": "free" if free else "pro",
            },
        )
