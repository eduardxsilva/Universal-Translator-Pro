"""Gerenciamento de configurações locais sem armazenamento de segredos em texto puro."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..domain.enums import DeepLAccountType, FormalityLevel, ProviderType


def get_app_data_dir() -> Path:
    """Retorna o diretório padrão de dados do aplicativo compatível com Windows 10/11 e POSIX."""
    override = os.environ.get("UNIVERSAL_TRANSLATOR_DATA_DIR")
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        base = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
        folder = base / "UniversalTranslator"
    else:
        folder = Path.home() / ".config" / "UniversalTranslator"

    folder.mkdir(parents=True, exist_ok=True)
    return folder


from ..infrastructure.security.credential_manager import CredentialManager  # noqa: E402


@dataclass
class AppSettings:
    language: str = "English (EUA)"
    target_language_code: str = "EN-US"
    source_language: str = "Português (Brasil)"
    source_language_code: str = "PT-BR"
    auto_detect_source: bool = True
    incoming_language: str = "Português (Brasil)"
    incoming_language_code: str = "PT-BR"
    active_provider: str = ProviderType.DEEPL.value
    fallback_provider: str = ProviderType.DEEPL.value
    deepl_account_type: str = DeepLAccountType.AUTO.value
    formality: str = FormalityLevel.DEFAULT.value
    show_floating_button: bool = True
    automatic_mode_enabled: bool = False
    floating_pos_x: int = -1
    floating_pos_y: int = -1
    capture_hotkey: str = "ctrl+shift+c"
    inline_translate_hotkey: str = "ctrl+shift+t"
    incoming_translate_hotkey: str = "ctrl+shift+r"
    enable_legacy_ctrl_a_hook: bool = False
    enable_ctrl_c_hook: bool = False
    auto_pretranslate_on_copy: bool = False
    prefer_uia_capture: bool = True
    allow_clipboard_fallback: bool = True
    preserve_cursor_position: bool = True
    protect_sensitive_fields: bool = True
    verify_before_replace: bool = True
    review_before_replace: bool = False
    privacy_no_history: bool = False
    start_with_windows: bool = False
    minimize_to_tray: bool = True
    history_retention_days: int = 30
    cache_ttl_days: int = 30
    request_timeout_seconds: float = 15.0
    auto_idle_timeout_seconds: int = 5
    deepl_alert_threshold_percent: int = 80
    max_retries: int = 2
    monthly_budget_usd: float = 25.0
    openai_model: str = "gpt-4o-mini"
    microsoft_region: str = "brazilsouth"
    theme: str = "dark"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_safe_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for forbidden in (
            "api_key",
            "deepl_api_key",
            "openai_api_key",
            "google_api_key",
            "microsoft_api_key",
        ):
            data.pop(forbidden, None)
            if isinstance(data.get("extra"), dict):
                data["extra"].pop(forbidden, None)
        return data


class SettingsManager:
    """Gerencia leitura e escrita atômica de settings.json + migração automática de chaves para o Cofre do Windows."""

    PROVIDER_SECRET_KEYS = {
        ProviderType.DEEPL.value: "deepl_api_key",
        ProviderType.GOOGLE.value: "google_api_key",
        ProviderType.MICROSOFT.value: "microsoft_api_key",
        ProviderType.OPENAI.value: "openai_api_key",
    }

    def __init__(
        self,
        settings_path: Path | None = None,
        credential_manager: CredentialManager | None = None,
    ) -> None:
        self.settings_path = settings_path or (get_app_data_dir() / "settings.json")
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.credentials = credential_manager or CredentialManager()

    def load(self) -> AppSettings:
        settings = AppSettings()
        if not self.settings_path.exists():
            return settings

        try:
            with self.settings_path.open("r", encoding="utf-8") as fp:
                raw = json.load(fp)
            if not isinstance(raw, dict):
                return settings

            legacy_api_key = raw.pop("api_key", None)
            if isinstance(legacy_api_key, str) and legacy_api_key.strip():
                self.credentials.set_secret("deepl_api_key", legacy_api_key.strip())
                self._write_clean_dict(raw)

            for k, v in raw.items():
                if k.endswith("_api_key") and isinstance(v, str) and v.strip():
                    self.credentials.set_secret(k, v.strip())
                    continue
                if hasattr(settings, k):
                    setattr(settings, k, v)
                else:
                    settings.extra[k] = v
        except (OSError, json.JSONDecodeError):
            pass

        return settings

    def save(self, settings: AppSettings | dict[str, Any]) -> None:
        if isinstance(settings, dict):
            api_key = settings.get("api_key", "")
            if isinstance(api_key, str) and api_key.strip():
                self.credentials.set_secret("deepl_api_key", api_key.strip())

            current = self.load()
            for k, v in settings.items():
                if k == "api_key" or k.endswith("_api_key"):
                    if isinstance(v, str) and v.strip():
                        secret_name = "deepl_api_key" if k == "api_key" else k
                        self.credentials.set_secret(secret_name, v.strip())
                    continue
                if hasattr(current, k):
                    setattr(current, k, v)
                else:
                    current.extra[k] = v
            payload = current.to_safe_dict()
        else:
            payload = settings.to_safe_dict()

        self._write_clean_dict(payload)

    def _write_clean_dict(self, payload: dict[str, Any]) -> None:
        clean_payload = dict(payload)
        for key in list(clean_payload.keys()):
            if key == "api_key" or key.endswith("_api_key"):
                clean_payload.pop(key, None)

        tmp_path = self.settings_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as fp:
            json.dump(clean_payload, fp, ensure_ascii=False, indent=2)
        tmp_path.replace(self.settings_path)

    def get_provider_api_key(self, provider: str | ProviderType) -> str:
        prov_val = provider.value if isinstance(provider, ProviderType) else str(provider).lower()
        secret_key = self.PROVIDER_SECRET_KEYS.get(prov_val, f"{prov_val}_api_key")
        return self.credentials.get_secret(secret_key)

    def set_provider_api_key(self, provider: str | ProviderType, api_key: str) -> bool:
        prov_val = provider.value if isinstance(provider, ProviderType) else str(provider).lower()
        secret_key = self.PROVIDER_SECRET_KEYS.get(prov_val, f"{prov_val}_api_key")
        if not api_key or not api_key.strip():
            return self.credentials.delete_secret(secret_key)
        return self.credentials.set_secret(secret_key, api_key.strip())
