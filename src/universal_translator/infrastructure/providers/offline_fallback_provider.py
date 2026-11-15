"""Provedor local de contingência (Offline Fallback) para manter a aplicação utilizável sem internet/API."""
from __future__ import annotations

import re
import time

from ...domain.contracts import TranslationProviderPort
from ...domain.entities import TranslationRequest, TranslationResult
from ...domain.enums import ProviderType
from ...domain.services import LanguageDetector


class OfflineFallbackProvider(TranslationProviderPort):
    """Provedor determinístico local de contingência quando provedores externos estão indisponíveis."""

    _PHRASEBOOK_PT_EN: dict[str, str] = {
        "você poderia me enviar o relatório amanhã?": "Could you send me the report tomorrow?",
        "você poderia me enviar o relatório amanhã": "Could you send me the report tomorrow",
        "bom dia": "Good morning",
        "boa tarde": "Good afternoon",
        "boa noite": "Good evening",
        "obrigado pelo retorno": "Thank you for your reply",
        "obrigado": "Thank you",
        "por favor": "Please",
        "estou em reunião agora": "I am in a meeting right now",
        "vou verificar e te aviso": "I will check and let you know",
        "tudo bem?": "How are you?",
        "até amanhã": "See you tomorrow",
    }

    _WORD_MAP_PT_EN: dict[str, str] = {
        "você": "you",
        "poderia": "could",
        "enviar": "send",
        "relatório": "report",
        "amanhã": "tomorrow",
        "hoje": "today",
        "reunião": "meeting",
        "projeto": "project",
        "dados": "data",
        "análise": "analysis",
        "ambiental": "environmental",
        "sistema": "system",
        "erro": "error",
        "arquivo": "file",
        "mensagem": "message",
        "equipe": "team",
        "cliente": "client",
        "urgente": "urgent",
    }

    _WORD_MAP_EN_PT: dict[str, str] = {v: k for k, v in _WORD_MAP_PT_EN.items()}
    _PHRASEBOOK_EN_PT: dict[str, str] = {v.lower(): k.capitalize() for k, v in _PHRASEBOOK_PT_EN.items()}

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.OFFLINE_FALLBACK

    @property
    def display_name(self) -> str:
        return "Contingência Local (Offline Fallback)"

    def is_configured(self) -> bool:
        return True

    def translate(self, request: TranslationRequest) -> TranslationResult:
        started = time.perf_counter()
        raw = (request.text or "").strip()
        detected = request.source_lang or LanguageDetector.detect(raw)
        target_upper = (request.target_lang or "EN-US").upper()

        normalized = raw.lower()
        if target_upper.startswith("EN") and normalized in self._PHRASEBOOK_PT_EN:
            translated = self._PHRASEBOOK_PT_EN[normalized]
        elif target_upper.startswith("PT") and normalized in self._PHRASEBOOK_EN_PT:
            translated = self._PHRASEBOOK_EN_PT[normalized]
        else:
            word_map = (
                self._WORD_MAP_PT_EN
                if target_upper.startswith("EN")
                else self._WORD_MAP_EN_PT
            )

            def _swap_word(match: re.Match[str]) -> str:
                w = match.group(0)
                low = w.lower()
                if low in word_map:
                    rep = word_map[low]
                    return rep.capitalize() if w[0].isupper() else rep
                return w

            translated = re.sub(r"\b[a-záàâãéêíóôõúüç]+\b", _swap_word, raw, flags=re.IGNORECASE)

        latency_ms = (time.perf_counter() - started) * 1000.0
        return TranslationResult(
            translated_text=translated,
            source_text=raw,
            detected_source_lang=detected,
            target_lang=request.target_lang,
            provider=ProviderType.OFFLINE_FALLBACK,
            cached=False,
            latency_ms=round(latency_ms, 2),
            characters_billed=0,
            estimated_cost_usd=0.0,
            direction=request.direction,
            app_profile_name=request.app_profile_name,
            operation_id=request.operation_id,
            metadata={"offline_mode": True},
        )
