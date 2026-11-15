"""Enumerações e tipos fundamentais do domínio do Universal Translator Pro."""
from __future__ import annotations

from enum import Enum


class ProviderType(str, Enum):
    DEEPL = "deepl"
    GOOGLE = "google"
    MICROSOFT = "microsoft"
    OPENAI = "openai"
    OFFLINE_FALLBACK = "offline_fallback"


class DeepLAccountType(str, Enum):
    AUTO = "auto"
    FREE = "free"
    PRO = "pro"


class FormalityLevel(str, Enum):
    DEFAULT = "default"
    FORMAL = "formal"
    INFORMAL = "informal"
    BUSINESS = "prefer_more"
    CASUAL = "prefer_less"


class TranslationDirection(str, Enum):
    OUTGOING_SENT = "outgoing_sent"          # Usuário escreve -> traduz para idioma de destino
    INCOMING_RECEIVED = "incoming_received"  # Usuário seleciona mensagem estrangeira -> traduz para idioma de leitura
    INLINE_REPLACE = "inline_replace"        # Tradução inline no campo de texto ativo
    CONTEXTUAL_CHAT = "contextual_chat"      # Tradução com preservação de contexto conversacional


class CaptureMethod(str, Enum):
    UI_AUTOMATION = "ui_automation"
    WIN32_MESSAGE = "win32_message"
    CONTROLLED_CLIPBOARD = "controlled_clipboard"
    MANUAL_BUFFER = "manual_buffer"


class ControlAccessibilityKind(str, Enum):
    EDITABLE_FIELD = "editable_field"
    SELECTABLE_TEXT = "selectable_text"
    ACCESSIBLE_ELEMENT = "accessible_element"
    SENSITIVE_OR_PASSWORD = "sensitive_or_password"
    REQUIRES_OCR_OR_UNACCESSIBLE = "requires_ocr_or_unaccessible"


class ReplacementSafetyStatus(str, Enum):
    SAFE = "safe"
    WINDOW_INVALID = "window_invalid"
    FOCUS_CHANGED = "focus_changed"
    CONTENT_MISMATCH = "content_mismatch"
    PRIVILEGE_MISMATCH = "privilege_mismatch"
    BLOCKED_BY_USER = "blocked_by_user"
    EMPTY_TRANSLATION = "empty_translation"
    SENSITIVE_FIELD = "sensitive_field"
    NOT_EDITABLE = "not_editable"
    ALREADY_TRANSLATED = "already_translated"
    TYPING_IN_PROGRESS = "typing_in_progress"


class FloatingButtonState(str, Enum):
    IDLE = "idle"
    CAPTURED = "captured"
    TRANSLATING = "translating"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class LogCategory(str, Enum):
    APPLICATION = "Application"
    TRANSLATION = "Translation"
    CAPTURE = "Capture"
    CLIPBOARD = "Clipboard"
    ACCESSIBILITY = "Accessibility"
    WINDOWS_INTEGRATION = "WindowsIntegration"
    PROVIDER = "Provider"
    SECURITY = "Security"
    UI = "UI"


SUPPORTED_LANGUAGES: dict[str, str] = {
    "Português (Brasil)": "PT-BR",
    "Português (Portugal)": "PT-PT",
    "English (EUA)": "EN-US",
    "English (Reino Unido)": "EN-GB",
    "Español": "ES",
    "Français": "FR",
    "Deutsch": "DE",
    "Italiano": "IT",
    "日本語": "JA",
    "中文 (Simplificado)": "ZH",
    "Nederlands": "NL",
    "Polski": "PL",
    "Русский": "RU",
    "Українська": "UK",
    "한국어": "KO",
    "Svenska": "SV",
    "Dansk": "DA",
    "Norsk (Bokmål)": "NB",
    "Suomi": "FI",
    "Čeština": "CS",
    "Ελληνικά": "EL",
    "Türkçe": "TR",
    "العربية": "AR",
    "Bahasa Indonesia": "ID",
}

SOURCE_LANGUAGES: dict[str, str] = {
    "Detecção Automática": "AUTO",
    **SUPPORTED_LANGUAGES,
}

LANGUAGE_CODE_TO_NAME: dict[str, str] = {
    "AUTO": "Detecção Automática",
    "PT": "Português (Brasil)",
    "EN": "English (EUA)",
    **{v: k for k, v in SUPPORTED_LANGUAGES.items()},
}
