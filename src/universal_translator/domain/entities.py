"""Entidades de domínio ricas e imutáveis/controladas do Universal Translator Pro."""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .enums import (
    CaptureMethod,
    ControlAccessibilityKind,
    FormalityLevel,
    ProviderType,
    ReplacementSafetyStatus,
    TranslationDirection,
)


def generate_operation_id(prefix: str = "op") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def compute_text_hash(text: str) -> str:
    normalized = (text or "").strip().replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class GlossaryEntry:
    source_term: str
    target_term: str
    source_lang: str = "ANY"
    target_lang: str = "ANY"
    case_sensitive: bool = False
    do_not_translate: bool = False
    domain_tag: str = "geral"
    priority: int = 10
    enabled: bool = True
    notes: str = ""
    id: int | None = None


@dataclass
class ApplicationProfile:
    app_name: str
    process_names: list[str] = field(default_factory=list)
    window_title_patterns: list[str] = field(default_factory=list)
    source_lang: str = "PT-BR"
    target_lang: str = "EN-US"
    incoming_target_lang: str = "PT-BR"
    auto_translate_selection: bool = False
    translate_outgoing: bool = True
    inline_hotkey: str = "ctrl+shift+t"
    capture_hotkey: str = "ctrl+shift+c"
    formality: FormalityLevel = FormalityLevel.DEFAULT
    review_before_replace: bool = False
    floating_button_behavior: str = "always_visible"  # always_visible, show_on_capture, hidden
    preferred_provider: ProviderType = ProviderType.DEEPL
    custom_glossary_domain: str = "geral"
    preserve_context: bool = True
    enabled: bool = True
    id: int | None = None


@dataclass
class WindowContext:
    hwnd: int
    process_id: int = 0
    process_name: str = ""
    window_title: str = ""
    class_name: str = ""
    thread_id: int = 0
    is_maximized: bool = False
    is_minimized: bool = False
    is_elevated: bool = False
    captured_at: float = field(default_factory=time.time)
    selection_hash: str = ""
    matched_profile_name: str = "Padrão Global"
    focused_control_hwnd: int = 0
    focused_control_id: str = ""
    control_kind: str = ControlAccessibilityKind.EDITABLE_FIELD.value
    is_password_or_sensitive: bool = False
    cursor_position: tuple[int, int] | None = None


@dataclass
class EditableFieldSnapshot:
    hwnd: int
    focused_control_hwnd: int
    focused_control_id: str
    text: str
    text_hash: str
    cursor_range: tuple[int, int] | None
    method: CaptureMethod
    captured_at: float = field(default_factory=time.time)
    window_context: WindowContext | None = None


@dataclass
class CaptureResult:
    success: bool
    text: str
    window_context: WindowContext | None
    method: CaptureMethod
    error_message: str = ""
    duration_ms: float = 0.0
    operation_id: str = field(default_factory=lambda: generate_operation_id("cap"))


@dataclass
class ReplacementResult:
    success: bool
    safety_status: ReplacementSafetyStatus
    method_used: CaptureMethod
    original_text: str
    new_text: str
    hwnd: int | None
    rollback_available: bool
    message: str
    duration_ms: float = 0.0
    operation_id: str = field(default_factory=lambda: generate_operation_id("rep"))


@dataclass
class ConversationTurn:
    source_text: str
    translated_text: str
    source_lang: str
    target_lang: str
    direction: TranslationDirection
    timestamp: float = field(default_factory=time.time)


@dataclass
class TranslationRequest:
    text: str
    target_lang: str
    source_lang: str | None = None
    provider: ProviderType = ProviderType.DEEPL
    formality: FormalityLevel = FormalityLevel.DEFAULT
    direction: TranslationDirection = TranslationDirection.OUTGOING_SENT
    app_profile_name: str = "Padrão Global"
    glossary_domain: str = "geral"
    context_turns: list[ConversationTurn] = field(default_factory=list)
    privacy_no_history: bool = False
    bypass_cache: bool = False
    operation_id: str = field(default_factory=lambda: generate_operation_id("trn"))


@dataclass
class TranslationResult:
    translated_text: str
    source_text: str
    detected_source_lang: str
    target_lang: str
    provider: ProviderType
    cached: bool = False
    latency_ms: float = 0.0
    characters_billed: int = 0
    estimated_cost_usd: float = 0.0
    glossary_matches: list[str] = field(default_factory=list)
    direction: TranslationDirection = TranslationDirection.OUTGOING_SENT
    app_profile_name: str = "Padrão Global"
    operation_id: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TranslationHistoryEntry:
    operation_id: str
    source_text: str
    translated_text: str
    source_lang: str
    target_lang: str
    provider: str
    app_name: str
    direction: str
    cached: bool
    characters_count: int
    estimated_cost_usd: float
    latency_ms: float
    created_at: float = field(default_factory=time.time)
    id: int | None = None


@dataclass
class ProviderMetrics:
    provider: str
    total_requests: int = 0
    cache_hits: int = 0
    failed_requests: int = 0
    total_characters: int = 0
    estimated_cost_usd: float = 0.0
    avg_latency_ms: float = 0.0
    circuit_state: str = "CLOSED"
    last_error: str = ""
