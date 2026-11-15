"""Contratos e portas (Interfaces) da camada de domínio (Clean Architecture)."""
from __future__ import annotations

from abc import ABC, abstractmethod

from .entities import (
    ApplicationProfile,
    CaptureResult,
    GlossaryEntry,
    ReplacementResult,
    TranslationHistoryEntry,
    TranslationRequest,
    TranslationResult,
    WindowContext,
)
from .enums import ProviderType


class TranslationProviderPort(ABC):
    """Contrato independente de fornecedor para provedores de tradução."""

    @property
    @abstractmethod
    def provider_type(self) -> ProviderType:
        raise NotImplementedError

    @property
    @abstractmethod
    def display_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def is_configured(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def translate(self, request: TranslationRequest) -> TranslationResult:
        raise NotImplementedError


class SecretStorePort(ABC):
    """Contrato para armazenamento seguro de chaves de API e segredos."""

    @abstractmethod
    def get_secret(self, key_name: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def set_secret(self, key_name: str, secret_value: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def delete_secret(self, key_name: str) -> bool:
        raise NotImplementedError

    @property
    @abstractmethod
    def backend_name(self) -> str:
        raise NotImplementedError


class WindowInspectorPort(ABC):
    """Contrato para inspeção e gerenciamento de foco de janelas no Windows."""

    @abstractmethod
    def get_foreground_window_context(self) -> WindowContext | None:
        raise NotImplementedError

    @abstractmethod
    def is_window_valid(self, hwnd: int) -> bool:
        raise NotImplementedError

    @abstractmethod
    def activate_window_safely(self, context: WindowContext) -> bool:
        raise NotImplementedError


class TextCapturePort(ABC):
    """Contrato para subsistema de captura e substituição segura de texto."""

    @abstractmethod
    def capture_selection(self, target_hwnd: int | None = None) -> CaptureResult:
        raise NotImplementedError

    @abstractmethod
    def replace_text_safely(
        self,
        new_text: str,
        original_context: WindowContext,
        expected_original_text: str = "",
        verify_content_before_paste: bool = True,
    ) -> ReplacementResult:
        raise NotImplementedError

    @abstractmethod
    def rollback_last_replacement(self) -> ReplacementResult:
        raise NotImplementedError


class PersistenceRepositoryPort(ABC):
    """Contrato para persistência SQLite (Histórico, Cache, Glossário, Perfis e Métricas)."""

    @abstractmethod
    def get_cached_translation(self, cache_key: str) -> TranslationResult | None:
        raise NotImplementedError

    @abstractmethod
    def save_cached_translation(
        self, cache_key: str, result: TranslationResult, ttl_seconds: int = 86400 * 30
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def add_history(self, entry: TranslationHistoryEntry) -> int:
        raise NotImplementedError

    @abstractmethod
    def list_history(self, limit: int = 100, search_query: str = "") -> list[TranslationHistoryEntry]:
        raise NotImplementedError

    @abstractmethod
    def clear_history(self, older_than_days: int | None = None) -> int:
        raise NotImplementedError

    @abstractmethod
    def list_glossary(self) -> list[GlossaryEntry]:
        raise NotImplementedError

    @abstractmethod
    def upsert_glossary_entry(self, entry: GlossaryEntry) -> int:
        raise NotImplementedError

    @abstractmethod
    def delete_glossary_entry(self, entry_id: int) -> bool:
        raise NotImplementedError

    @abstractmethod
    def list_profiles(self) -> list[ApplicationProfile]:
        raise NotImplementedError

    @abstractmethod
    def upsert_profile(self, profile: ApplicationProfile) -> int:
        raise NotImplementedError

    @abstractmethod
    def delete_profile(self, profile_id: int) -> bool:
        raise NotImplementedError
