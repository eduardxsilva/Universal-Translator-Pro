"""Camada de Infraestrutura do Universal Translator."""
from .accessibility import UIAutomationAdapter
from .clipboard import SafeTextCaptureEngine
from .logging import DiagnosticRecord, ObservabilityLogger, PrivacyRedactingFilter
from .persistence import SQLiteRepository
from .providers import (
    CircuitBreaker,
    CostEstimator,
    DeepLProvider,
    GoogleTranslateProvider,
    MicrosoftTranslatorProvider,
    OfflineFallbackProvider,
    OpenAIProvider,
    RetryPolicy,
    TranslationProviderError,
)
from .security import CredentialManager
from .windows import Win32WindowInspector

__all__ = [
    "CircuitBreaker",
    "CostEstimator",
    "CredentialManager",
    "DeepLProvider",
    "DiagnosticRecord",
    "GoogleTranslateProvider",
    "MicrosoftTranslatorProvider",
    "ObservabilityLogger",
    "OfflineFallbackProvider",
    "OpenAIProvider",
    "PrivacyRedactingFilter",
    "RetryPolicy",
    "SQLiteRepository",
    "SafeTextCaptureEngine",
    "TranslationProviderError",
    "UIAutomationAdapter",
    "Win32WindowInspector",
]
