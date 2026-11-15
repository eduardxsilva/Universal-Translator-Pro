"""Fachada retrocompatível de tradução delegando para a arquitetura multi-provedor."""
from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from universal_translator.domain.entities import TranslationRequest  # noqa: E402
from universal_translator.domain.enums import ProviderType  # noqa: E402
from universal_translator.infrastructure.providers.deepl_provider import DeepLProvider  # noqa: E402
from universal_translator.infrastructure.providers.resilience import (  # noqa: E402
    TranslationProviderError,
)


def translate_text(
    text: str,
    target_language: str,
    api_key: str,
    source_language: str | None = None,
) -> str:
    if not api_key or not api_key.strip():
        raise ValueError("Configure sua chave da API DeepL.")
    if not text or not text.strip():
        raise ValueError("Não há texto para traduzir.")

    provider = DeepLProvider(api_key_resolver=lambda: api_key.strip())
    req = TranslationRequest(
        text=text,
        target_lang=target_language,
        source_lang=source_language,
        provider=ProviderType.DEEPL,
    )
    try:
        res = provider.translate(req)
        return res.translated_text
    except TranslationProviderError as exc:
        raise RuntimeError(str(exc)) from exc
