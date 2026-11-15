"""Motor Inteligente de Monitoramento e Tradução Automática com Proteção Contra Interferências."""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..domain.entities import (
    CaptureResult,
    ReplacementResult,
    TranslationResult,
    compute_text_hash,
)
from ..domain.enums import TranslationDirection


@dataclass
class FieldTranslationGuardState:
    """Rastreia o último estado traduzido de um campo específico (hwnd + control_id) para impedir loops."""

    hwnd: int
    control_id: str
    last_source_hash: str = ""
    last_translated_hash: str = ""
    last_translated_at: float = 0.0


@dataclass
class AutoCycleOutcome:
    """Resultado auditável de um ciclo de monitoramento automático após inatividade."""

    executed: bool
    skipped_reason: str = ""
    capture: CaptureResult | None = None
    translation: TranslationResult | None = None
    replacement: ReplacementResult | None = None
    serial: int = 0
    timestamp: float = field(default_factory=time.time)


class AutoTranslationMonitor:
    """Gerencia o ciclo de vida do Modo de Tradução Automática (Seção 3 da especificação).

    Responsabilidades:
    1. Monitorar eventos relevantes de edição (digitação, Backspace, Delete, colagem, seleção, mudanças de conteúdo).
    2. Reiniciar o temporizador inteligente de 5s a cada nova interação relevante.
    3. Ignorar simples ausência de movimento do mouse sem edição prévia.
    4. Cancelar imediatamente capturas ou traduções pendentes quando o usuário muda de janela, muda de campo
       ou volta a digitar.
    5. Impedir retradução da mesma mensagem ou tradução do próprio resultado já inserido.
    6. Proteger atalhos de sistema e campos de senha/sensíveis.
    """

    EDITING_KEYS = {
        "space",
        "backspace",
        "delete",
        "enter",
        "return",
        "tab",
    }

    NAVIGATION_OR_SELECTION_KEYS = {
        "left",
        "right",
        "up",
        "down",
        "home",
        "end",
        "page up",
        "page down",
    }

    NON_TEXT_SYSTEM_KEYS = {
        "esc",
        "escape",
        "f1",
        "f2",
        "f3",
        "f4",
        "f5",
        "f6",
        "f7",
        "f8",
        "f9",
        "f10",
        "f11",
        "f12",
        "print screen",
        "insert",
        "caps lock",
        "num lock",
        "scroll lock",
        "left windows",
        "right windows",
        "win",
    }

    def __init__(
        self,
        orchestrator: Any,
        idle_timeout_seconds: float = 5.0,
        on_timer_restart: Callable[[int], None] | None = None,
        on_timer_stop: Callable[[], None] | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.idle_timeout_seconds = max(1.0, float(idle_timeout_seconds))
        self._on_timer_restart = on_timer_restart
        self._on_timer_stop = on_timer_stop

        self._lock = threading.RLock()
        self._enabled: bool = False
        self._internal_action: bool = False
        self._activity_serial: int = 0
        self._active_hwnd: int | None = None
        self._active_control_id: str = ""
        self._has_pending_edit_content: bool = False
        self._last_event_timestamp: float = 0.0
        self._field_guards: dict[tuple[int, str], FieldTranslationGuardState] = {}

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def activity_serial(self) -> int:
        with self._lock:
            return self._activity_serial

    @property
    def active_hwnd(self) -> int | None:
        with self._lock:
            return self._active_hwnd

    @property
    def active_control_id(self) -> str:
        with self._lock:
            return self._active_control_id

    @property
    def has_pending_edit(self) -> bool:
        with self._lock:
            return self._has_pending_edit_content

    @property
    def internal_action(self) -> bool:
        with self._lock:
            return self._internal_action

    @internal_action.setter
    def internal_action(self, value: bool) -> None:
        with self._lock:
            self._internal_action = bool(value)

    def set_idle_timeout_seconds(self, seconds: float) -> None:
        with self._lock:
            self.idle_timeout_seconds = max(1.0, float(seconds))

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._activity_serial += 1
            if not self._enabled:
                self._active_hwnd = None
                self._active_control_id = ""
                self._has_pending_edit_content = False
                if self._on_timer_stop:
                    self._on_timer_stop()

    def close_and_stop(self) -> None:
        """Acionado ao clicar no X do botão flutuante: desativa o monitoramento e impede novas traduções."""
        self.set_enabled(False)

    def _restart_timer_locked(self) -> int:
        self._activity_serial += 1
        self._last_event_timestamp = time.monotonic()
        serial = self._activity_serial
        if self._on_timer_restart:
            self._on_timer_restart(int(self.idle_timeout_seconds * 1000))
        return serial

    def _cancel_pending_locked(self) -> int:
        self._activity_serial += 1
        self._has_pending_edit_content = False
        if self._on_timer_stop:
            self._on_timer_stop()
        return self._activity_serial

    def on_window_or_field_changed(self, new_hwnd: int | None, new_control_id: str = "") -> bool:
        """Cancela o temporizador/tradução em andamento se o usuário mudou de janela ou de campo."""
        with self._lock:
            if not self._enabled or self._internal_action:
                return False
            hwnd_changed = (
                self._active_hwnd is not None
                and new_hwnd is not None
                and int(new_hwnd) != int(self._active_hwnd)
            )
            ctrl_changed = (
                bool(self._active_control_id)
                and bool(new_control_id)
                and new_control_id != self._active_control_id
            )
            if hwnd_changed or ctrl_changed:
                self._active_hwnd = int(new_hwnd) if new_hwnd else None
                self._active_control_id = new_control_id or ""
                self._cancel_pending_locked()
                return True
            return False

    def on_key_event(
        self,
        key_name: str,
        hwnd: int | None,
        control_id: str = "",
        ctrl_pressed: bool = False,
        alt_pressed: bool = False,
    ) -> bool:
        """Processa um evento de teclado e reinicia o temporizador de 5s quando relevante."""
        with self._lock:
            if not self._enabled or self._internal_action or not hwnd:
                return False

            k = (key_name or "").strip().lower()
            if not k or k in self.NON_TEXT_SYSTEM_KEYS:
                return False

            if self._active_hwnd is not None and int(hwnd) != int(self._active_hwnd):
                self._active_hwnd = int(hwnd)
                self._active_control_id = control_id or f"hwnd:{hwnd}"
                self._has_pending_edit_content = False

            if alt_pressed:
                self._cancel_pending_locked()
                return False

            if ctrl_pressed:
                if k in ("v", "x", "z", "y"):
                    self._active_hwnd = int(hwnd)
                    if control_id:
                        self._active_control_id = control_id
                    self._has_pending_edit_content = True
                    self._restart_timer_locked()
                    return True
                if k in ("a", "c"):
                    if self._has_pending_edit_content:
                        self._restart_timer_locked()
                        return True
                return False

            is_editing_key = (len(k) == 1) or (k in self.EDITING_KEYS)
            is_nav_key = k in self.NAVIGATION_OR_SELECTION_KEYS

            if is_editing_key:
                self._active_hwnd = int(hwnd)
                if control_id:
                    self._active_control_id = control_id
                self._has_pending_edit_content = True
                self._restart_timer_locked()
                return True

            if is_nav_key and self._has_pending_edit_content:
                self._restart_timer_locked()
                return True

            return False

    def on_paste_or_content_event(self, hwnd: int | None, control_id: str = "") -> bool:
        """Notifica colagem, alteração de conteúdo via acessibilidade ou edição relevante."""
        with self._lock:
            if not self._enabled or self._internal_action or not hwnd:
                return False
            self._active_hwnd = int(hwnd)
            if control_id:
                self._active_control_id = control_id
            self._has_pending_edit_content = True
            self._restart_timer_locked()
            return True

    def on_mouse_interaction(self, event_kind: str, hwnd: int | None, control_id: str = "") -> bool:
        """Cliques/seleção de mouse reiniciam o timer se houver edição pendente, ou cancelam se trocou de janela/campo.
        Simples ausência ou movimento de mouse NUNCA dispara tradução.
        """
        with self._lock:
            if not self._enabled or self._internal_action:
                return False
            if event_kind not in ("ButtonEvent", "WheelEvent", "click", "selection"):
                return False
            if self.on_window_or_field_changed(hwnd, control_id):
                return False
            if self._has_pending_edit_content:
                self._restart_timer_locked()
                return True
            return False

    def is_text_eligible(
        self,
        text: str,
        hwnd: int,
        control_id: str,
        source_lang: str | None,
        target_lang: str,
    ) -> tuple[bool, str]:
        """Valida se o texto capturado é elegível para tradução e não viola as proteções da seção 3.4."""
        clean = (text or "").strip()
        if len(clean) < 2:
            return False, "Texto muito curto ou vazio."

        if not re.search(r"[^\W\d_]", clean, re.UNICODE):
            return False, "Texto contém apenas números, pontuação ou símbolos."

        if re.match(r"^https?://\S+$", clean, re.IGNORECASE):
            return False, "Texto é apenas uma URL."

        text_hash = compute_text_hash(clean)
        guard_key = (int(hwnd), control_id or f"hwnd:{hwnd}")
        with self._lock:
            guard = self._field_guards.get(guard_key)
            if guard is not None:
                if text_hash == guard.last_translated_hash:
                    return False, "O conteúdo atual já é o próprio resultado da tradução anterior."
                if text_hash == guard.last_source_hash:
                    return False, "Esta mensagem já foi traduzida neste campo."

        tgt_base = (target_lang or "EN-US").strip().upper().split("-")[0]
        if source_lang and source_lang.strip().upper() not in ("AUTO", "ANY", ""):
            src_base = source_lang.strip().upper().split("-")[0]
            if src_base == tgt_base:
                return False, "Idioma de origem configurado é idêntico ao idioma de destino."

        return True, "OK"

    def record_successful_replacement(
        self,
        hwnd: int,
        control_id: str,
        original_text: str,
        translated_text: str,
    ) -> None:
        guard_key = (int(hwnd), control_id or f"hwnd:{hwnd}")
        with self._lock:
            self._field_guards[guard_key] = FieldTranslationGuardState(
                hwnd=int(hwnd),
                control_id=control_id or f"hwnd:{hwnd}",
                last_source_hash=compute_text_hash(original_text),
                last_translated_hash=compute_text_hash(translated_text),
                last_translated_at=time.time(),
            )
            self._has_pending_edit_content = False

    def was_interrupted(self, expected_serial: int, expected_hwnd: int, expected_control_id: str = "") -> bool:
        """Verifica se houve nova digitação, desativação ou mudança de foco/janela desde o início do ciclo."""
        with self._lock:
            if not self._enabled:
                return True
            if self._activity_serial != expected_serial:
                return True
            if self._active_hwnd is not None and int(self._active_hwnd) != int(expected_hwnd):
                return True
            if (
                expected_control_id
                and self._active_control_id
                and self._active_control_id != expected_control_id
            ):
                return True
            return False

    def execute_idle_cycle(
        self,
        source_lang: str | None = None,
        target_lang: str | None = None,
    ) -> AutoCycleOutcome:
        """Executa o fluxo completo da Seção 3.1 após os 5 segundos de inatividade."""
        with self._lock:
            if not self._enabled or self._internal_action:
                return AutoCycleOutcome(executed=False, skipped_reason="Modo automático inativo.")
            if not self._has_pending_edit_content or not self._active_hwnd:
                return AutoCycleOutcome(executed=False, skipped_reason="Nenhuma edição pendente.")
            serial = self._activity_serial
            hwnd = int(self._active_hwnd)
            ctrl_id = self._active_control_id

        orch = self.orchestrator
        inspector = orch.capture_engine.inspector
        fg_now = inspector.get_foreground_hwnd()
        if fg_now is None or int(fg_now) != hwnd:
            return AutoCycleOutcome(
                executed=False,
                skipped_reason="O usuário mudou de janela antes da captura.",
                serial=serial,
            )

        self.internal_action = True
        try:
            capture: CaptureResult = orch.capture_engine.capture_entire_field(
                target_hwnd=hwnd,
                prefer_uia=orch.settings.prefer_uia_capture,
                allow_clipboard_fallback=orch.settings.allow_clipboard_fallback,
            )
        finally:
            self.internal_action = False

        if not capture.success or not capture.text.strip():
            return AutoCycleOutcome(
                executed=False,
                skipped_reason=capture.error_message or "Nenhum texto capturado.",
                capture=capture,
                serial=serial,
            )

        resolved_ctrl_id = (
            capture.window_context.focused_control_id
            if capture.window_context and capture.window_context.focused_control_id
            else (ctrl_id or f"hwnd:{hwnd}")
        )
        with self._lock:
            if not self._active_control_id:
                self._active_control_id = resolved_ctrl_id

        if self.was_interrupted(serial, hwnd, resolved_ctrl_id):
            return AutoCycleOutcome(
                executed=False,
                skipped_reason="Interação do usuário detectada durante a captura; operação cancelada.",
                capture=capture,
                serial=serial,
            )

        resolved_target = target_lang or orch.settings.target_language_code or "EN-US"
        resolved_source = (
            None
            if orch.settings.auto_detect_source or (source_lang and source_lang.upper() == "AUTO")
            else (source_lang or orch.settings.source_language_code)
        )

        eligible, reason = self.is_text_eligible(
            capture.text,
            hwnd=hwnd,
            control_id=resolved_ctrl_id,
            source_lang=resolved_source,
            target_lang=resolved_target,
        )
        if not eligible:
            return AutoCycleOutcome(
                executed=False,
                skipped_reason=reason,
                capture=capture,
                serial=serial,
            )

        translation: TranslationResult = orch.translate(
            text=capture.text,
            source_lang=resolved_source,
            target_lang=resolved_target,
            direction=TranslationDirection.OUTGOING_SENT,
            window_context=capture.window_context,
        )

        if compute_text_hash(translation.translated_text) == compute_text_hash(capture.text):
            self.record_successful_replacement(
                hwnd, resolved_ctrl_id, capture.text, translation.translated_text
            )
            return AutoCycleOutcome(
                executed=False,
                skipped_reason="Texto traduzido é idêntico ao original.",
                capture=capture,
                translation=translation,
                serial=serial,
            )

        if self.was_interrupted(serial, hwnd, resolved_ctrl_id):
            return AutoCycleOutcome(
                executed=False,
                skipped_reason="Nova digitação ou mudança de foco durante a tradução; substituição descartada.",
                capture=capture,
                translation=translation,
                serial=serial,
            )

        target_ctx = capture.window_context
        if target_ctx is None:
            return AutoCycleOutcome(
                executed=False,
                skipped_reason="Contexto de janela ausente.",
                capture=capture,
                translation=translation,
                serial=serial,
            )

        self.internal_action = True
        try:
            replacement: ReplacementResult = orch.capture_engine.replace_entire_field_safely(
                new_text=translation.translated_text,
                original_context=target_ctx,
                expected_original_text=capture.text,
                verify_content_before_paste=orch.settings.verify_before_replace,
                check_interrupt_fn=lambda: self.was_interrupted(serial, hwnd, resolved_ctrl_id),
            )
        finally:
            self.internal_action = False

        if replacement.success:
            self.record_successful_replacement(
                hwnd, resolved_ctrl_id, capture.text, translation.translated_text
            )

        return AutoCycleOutcome(
            executed=replacement.success,
            skipped_reason="" if replacement.success else replacement.message,
            capture=capture,
            translation=translation,
            replacement=replacement,
            serial=serial,
        )
