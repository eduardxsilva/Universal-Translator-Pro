"""Fachada retrocompatível de configurações com proteção de segredos via CredentialManager."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from universal_translator.config.settings_manager import (  # noqa: E402
    SettingsManager,
    get_app_data_dir,
)
from universal_translator.domain.enums import ProviderType  # noqa: E402


def get_settings_path() -> Path:
    return get_app_data_dir() / "settings.json"


def load_settings() -> dict[str, Any]:
    mgr = SettingsManager(settings_path=get_settings_path())
    app_settings = mgr.load()
    data = app_settings.to_safe_dict()
    # Recupera a chave do cofre seguro apenas em memória quando solicitada, sem nunca gravá-la em settings.json
    data["api_key"] = mgr.get_provider_api_key(ProviderType.DEEPL)
    return data


def save_settings(settings: dict[str, Any]) -> None:
    mgr = SettingsManager(settings_path=get_settings_path())
    mgr.save(settings)
