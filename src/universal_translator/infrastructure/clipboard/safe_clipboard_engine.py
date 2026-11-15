"""Motor de captura e substituição segura de texto no Windows com UI Automation prioritário e Clipboard controlado."""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ...domain.contracts import TextCapturePort
from ...domain.entities import (
    CaptureResult,
    ReplacementResult,
    WindowContext,
    compute_text_hash,
    generate_operation_id,
)
from ...domain.enums import (
    CaptureMethod,
    ControlAccessibilityKind,
    LogCategory,
    ReplacementSafetyStatus,
)
from ..accessibility.ui_automation_adapter import UIAutomationAdapter
from ..logging.logger import ObservabilityLogger
from ..windows.win32_inspector import Win32WindowInspector

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_RIGHT = 0x27
VK_END = 0x23
VK_A = 0x41
VK_C = 0x43
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002


@dataclass
class _RollbackRecord:
    original_text: str
    replaced_text: str
    window_context: WindowContext
    timestamp: float


class SafeTextCaptureEngine(TextCapturePort):
    """Subsistema dedicado de captura e substituição segura de texto (seleção e campo integral).

    Prioriza:
    1. APIs nativas de acessibilidade (UI Automation `ValuePattern`/`TextPattern` e Win32 `WM_GETTEXT`/`EM_REPLACESEL`).
    2. Fallback controlado via área de transferência (`Ctrl+A` / `Ctrl+C` / `Ctrl+V`) com:
       - Validação estrita de foco (`hwnd` e `focused_control_id`) antes e depois.
       - Bloqueio automático em campos de senha e janelas sensíveis.
       - Restauração integral do clipboard original do usuário.
       - Preservação da posição do cursor ao final da edição.
    """

    MARKER_PREFIX = "__UT_CLIP_MARKER_"

    def __init__(
        self,
        window_inspector: Win32WindowInspector | None = None,
        uia_adapter: UIAutomationAdapter | None = None,
        logger: ObservabilityLogger | None = None,
        clipboard_read_fn: Callable[[], str] | None = None,
        clipboard_write_fn: Callable[[str], None] | None = None,
        hotkey_send_fn: Callable[[str, str], None] | None = None,
    ) -> None:
        self.inspector = window_inspector or Win32WindowInspector()
        self.logger = logger or ObservabilityLogger.get_instance()
        self.uia = uia_adapter or UIAutomationAdapter(self.inspector, self.logger)
        self._clipboard_lock = threading.RLock()
        self._last_rollback: _RollbackRecord | None = None
        self._has_custom_hotkey_fn = hotkey_send_fn is not None

        self._read_clip = clipboard_read_fn or self._default_clipboard_paste
        self._write_clip = clipboard_write_fn or self._default_clipboard_copy
        self._send_hotkey = hotkey_send_fn or self._default_send_hotkey

    @staticmethod
    def _default_clipboard_paste() -> str:
        for _ in range(5):
            try:
                import pyperclip  # type: ignore
                val = pyperclip.paste()
                if val is not None:
                    return str(val)
            except Exception:
                time.sleep(0.015)
        return ""

    @staticmethod
    def _default_clipboard_copy(text: str) -> None:
        last_err: Exception | None = None
        for _ in range(5):
            try:
                import pyperclip  # type: ignore
                pyperclip.copy(text)
                return
            except Exception as exc:
                last_err = exc
                time.sleep(0.02)
        if last_err is not None:
            raise RuntimeError(f"Falha ao acessar área de transferência: {last_err}") from last_err

    @staticmethod
    def _default_send_hotkey(mod: str, key: str) -> None:
        """Envia Ctrl+A, Ctrl+C, Ctrl+V ou tecla de cursor usando API nativa Win32 (keybd_event) + fallback."""
        if sys.platform == "win32":
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                if mod == "" and key.lower() in ("right", "end"):
                    vk = VK_RIGHT if key.lower() == "right" else VK_END
                    user32.keybd_event(vk, 0, 0, 0)
                    time.sleep(0.01)
                    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
                    return
                if mod.lower() == "ctrl" and key.lower() in ("a", "c", "v"):
                    vk_map = {"a": VK_A, "c": VK_C, "v": VK_V}
                    vk_letter = vk_map[key.lower()]
                    user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
                    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

                    user32.keybd_event(VK_CONTROL, 0, 0, 0)
                    time.sleep(0.015)
                    user32.keybd_event(vk_letter, 0, 0, 0)
                    time.sleep(0.02)
                    user32.keybd_event(vk_letter, 0, KEYEVENTF_KEYUP, 0)
                    time.sleep(0.015)
                    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
                    return
            except Exception:
                pass

        try:
            import pyautogui  # type: ignore
            pyautogui.PAUSE = 0
            pyautogui.FAILSAFE = False
            if mod:
                pyautogui.hotkey(mod, key, _pause=False)
            else:
                pyautogui.press(key, _pause=False)
        except Exception as exc:
            raise RuntimeError(f"Falha ao enviar atalho {mod}+{key}: {exc}") from exc

    def _enrich_context_with_focus_metadata(self, context: WindowContext) -> WindowContext:
        try:
            focus_hwnd, ctrl_id = self.uia.get_focused_control_identifier(context.hwnd)
            kind = self.uia.classify_focused_control(context.hwnd)
            context.focused_control_hwnd = focus_hwnd
            context.focused_control_id = ctrl_id
            context.control_kind = kind.value
            context.is_password_or_sensitive = (
                kind == ControlAccessibilityKind.SENSITIVE_OR_PASSWORD
                or self.inspector.is_sensitive_window_context(context)
            )
            context.cursor_position = self.uia.get_cursor_selection_range(context.hwnd)
        except Exception:
            pass
        return context

    def capture_entire_field(
        self,
        target_hwnd: int | None = None,
        prefer_uia: bool = True,
        allow_clipboard_fallback: bool = True,
    ) -> CaptureResult:
        """Captura o conteúdo integral do campo editável ativo (usado no Modo de Tradução Automática)."""
        op_id = generate_operation_id("fcap")
        started = time.perf_counter()

        hwnd = target_hwnd or self.inspector.get_foreground_hwnd()
        if not hwnd or not self.inspector.is_window_valid(int(hwnd)):
            return CaptureResult(
                success=False,
                text="",
                window_context=None,
                method=CaptureMethod.UI_AUTOMATION,
                error_message="Nenhuma janela ativa válida identificada.",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        # No modo automático, nunca força ativação de janela se o usuário já mudou de janela
        fg_now = self.inspector.get_foreground_hwnd()
        if fg_now is not None and int(fg_now) != int(hwnd):
            return CaptureResult(
                success=False,
                text="",
                window_context=None,
                method=CaptureMethod.UI_AUTOMATION,
                error_message="O foco mudou para outra janela antes da captura integral.",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        context = self.inspector.get_window_context_for_hwnd(int(hwnd))
        if context is None or self.inspector.is_own_process(context):
            return CaptureResult(
                success=False,
                text="",
                window_context=context,
                method=CaptureMethod.UI_AUTOMATION,
                error_message="A janela ativa pertence ao próprio Universal Translator ou é inválida.",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        self._enrich_context_with_focus_metadata(context)

        # Proteção rigorosa contra senhas e campos sensíveis
        if context.is_password_or_sensitive or self.uia.is_password_or_sensitive_control(int(hwnd)):
            self.logger.warning(
                LogCategory.SECURITY,
                "Captura automática bloqueada: campo de senha ou janela sensível detectada.",
                operation_id=op_id,
            )
            return CaptureResult(
                success=False,
                text="",
                window_context=context,
                method=CaptureMethod.UI_AUTOMATION,
                error_message="Campo de senha ou sensível detectado; captura bloqueada por segurança.",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        # Valida se o controle focado é efetivamente um campo editável
        if not self.uia.is_focused_editable_control(int(hwnd)):
            return CaptureResult(
                success=False,
                text="",
                window_context=context,
                method=CaptureMethod.UI_AUTOMATION,
                error_message=f"Elemento focado não é um campo editável ({context.control_kind}).",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        # Estratégia 1: Captura direta via UI Automation / Win32 Message (sem tocar no Clipboard)
        if prefer_uia and hasattr(self.uia, "try_capture_entire_field"):
            direct_text, direct_method, cursor_range = self.uia.try_capture_entire_field(int(hwnd))
            if direct_text and direct_text.strip() and direct_method is not None:
                context.selection_hash = compute_text_hash(direct_text)
                if cursor_range is not None:
                    context.cursor_position = cursor_range
                duration_ms = (time.perf_counter() - started) * 1000.0
                return CaptureResult(
                    success=True,
                    text=direct_text,
                    window_context=context,
                    method=direct_method,
                    duration_ms=round(duration_ms, 2),
                    operation_id=op_id,
                )

        if not allow_clipboard_fallback:
            return CaptureResult(
                success=False,
                text="",
                window_context=context,
                method=CaptureMethod.UI_AUTOMATION,
                error_message="Captura via UI Automation indisponível para este controle e fallback de clipboard desativado.",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        # Estratégia 2: Captura integral via Seleção + Clipboard Controlada com restauração de cursor e clipboard
        with self._clipboard_lock:
            previous_clipboard = ""
            try:
                previous_clipboard = self._read_clip()
            except Exception:
                previous_clipboard = ""

            sentinel = f"{self.MARKER_PREFIX}{op_id}__"
            copied_text = sentinel
            try:
                if self.inspector.get_foreground_hwnd() != int(hwnd):
                    return CaptureResult(
                        success=False,
                        text="",
                        window_context=context,
                        method=CaptureMethod.CONTROLLED_CLIPBOARD,
                        error_message="Janela perdeu o foco antes de selecionar o texto.",
                        duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                        operation_id=op_id,
                    )

                self._write_clip(sentinel)
                time.sleep(0.03 if not self._has_custom_hotkey_fn else 0.005)
                self._send_hotkey("ctrl", "a")
                time.sleep(0.05 if not self._has_custom_hotkey_fn else 0.005)
                self._send_hotkey("ctrl", "c")

                deadline = time.monotonic() + (1.0 if not self._has_custom_hotkey_fn else 0.15)
                while time.monotonic() < deadline:
                    current_val = self._read_clip()
                    if current_val and current_val != sentinel:
                        copied_text = current_val
                        break
                    time.sleep(0.025 if not self._has_custom_hotkey_fn else 0.005)

                # Desmarca a seleção posicionando o cursor ao final do texto sem alterá-lo
                try:
                    self._send_hotkey("", "right")
                except Exception:
                    pass

                if copied_text == sentinel or not copied_text.strip():
                    return CaptureResult(
                        success=False,
                        text="",
                        window_context=context,
                        method=CaptureMethod.CONTROLLED_CLIPBOARD,
                        error_message="Campo editável vazio ou sem conteúdo copiável.",
                        duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                        operation_id=op_id,
                    )

                context.selection_hash = compute_text_hash(copied_text)
                return CaptureResult(
                    success=True,
                    text=copied_text,
                    window_context=context,
                    method=CaptureMethod.CONTROLLED_CLIPBOARD,
                    duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                    operation_id=op_id,
                )
            except Exception as exc:
                return CaptureResult(
                    success=False,
                    text="",
                    window_context=context,
                    method=CaptureMethod.CONTROLLED_CLIPBOARD,
                    error_message=f"Falha ao capturar campo integral: {exc}",
                    duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                    operation_id=op_id,
                )
            finally:
                try:
                    if previous_clipboard and not previous_clipboard.startswith(self.MARKER_PREFIX):
                        self._write_clip(previous_clipboard)
                    else:
                        self._write_clip("")
                except Exception:
                    pass

    def replace_entire_field_safely(
        self,
        new_text: str,
        original_context: WindowContext,
        expected_original_text: str,
        verify_content_before_paste: bool = True,
        check_interrupt_fn: Callable[[], bool] | None = None,
    ) -> ReplacementResult:
        """Substitui o texto integral do campo editável com validação estrita de estado, foco e integridade."""
        op_id = generate_operation_id("frep")
        started = time.perf_counter()

        if not new_text or not new_text.strip():
            return ReplacementResult(
                success=False,
                safety_status=ReplacementSafetyStatus.EMPTY_TRANSLATION,
                method_used=CaptureMethod.UI_AUTOMATION,
                original_text=expected_original_text,
                new_text="",
                hwnd=original_context.hwnd if original_context else None,
                rollback_available=False,
                message="A tradução está vazia; substituição cancelada.",
                duration_ms=0.0,
                operation_id=op_id,
            )

        if check_interrupt_fn is not None and check_interrupt_fn():
            return ReplacementResult(
                success=False,
                safety_status=ReplacementSafetyStatus.TYPING_IN_PROGRESS,
                method_used=CaptureMethod.UI_AUTOMATION,
                original_text=expected_original_text,
                new_text=new_text,
                hwnd=original_context.hwnd if original_context else None,
                rollback_available=False,
                message="Nova digitação ou evento detectado; substituição cancelada para preservar o texto.",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        if original_context is None or not self.inspector.is_window_valid(original_context.hwnd):
            return ReplacementResult(
                success=False,
                safety_status=ReplacementSafetyStatus.WINDOW_INVALID,
                method_used=CaptureMethod.UI_AUTOMATION,
                original_text=expected_original_text,
                new_text=new_text,
                hwnd=original_context.hwnd if original_context else None,
                rollback_available=False,
                message="A janela original não está mais disponível.",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        # Verifica se o usuário mudou de janela
        current_fg = self.inspector.get_foreground_hwnd()
        if current_fg is None or int(current_fg) != int(original_context.hwnd):
            return ReplacementResult(
                success=False,
                safety_status=ReplacementSafetyStatus.FOCUS_CHANGED,
                method_used=CaptureMethod.UI_AUTOMATION,
                original_text=expected_original_text,
                new_text=new_text,
                hwnd=original_context.hwnd,
                rollback_available=False,
                message="O usuário mudou de janela; substituição cancelada.",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        # Verifica se o campo focado continua sendo exatamente o mesmo
        if hasattr(self.uia, "get_focused_control_identifier"):
            now_focus_hwnd, now_ctrl_id = self.uia.get_focused_control_identifier(original_context.hwnd)
            if (
                original_context.focused_control_hwnd
                and now_focus_hwnd
                and int(now_focus_hwnd) != int(original_context.focused_control_hwnd)
            ) or (
                original_context.focused_control_id
                and now_ctrl_id
                and now_ctrl_id != original_context.focused_control_id
            ):
                return ReplacementResult(
                    success=False,
                    safety_status=ReplacementSafetyStatus.FOCUS_CHANGED,
                    method_used=CaptureMethod.UI_AUTOMATION,
                    original_text=expected_original_text,
                    new_text=new_text,
                    hwnd=original_context.hwnd,
                    rollback_available=False,
                    message="O foco mudou para outro campo dentro da janela; substituição cancelada.",
                    duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                    operation_id=op_id,
                )

        if self.uia.is_password_or_sensitive_control(original_context.hwnd):
            return ReplacementResult(
                success=False,
                safety_status=ReplacementSafetyStatus.SENSITIVE_FIELD,
                method_used=CaptureMethod.UI_AUTOMATION,
                original_text=expected_original_text,
                new_text=new_text,
                hwnd=original_context.hwnd,
                rollback_available=False,
                message="Campo sensível/senha detectado; substituição bloqueada.",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        # Verifica se o conteúdo do campo permanece igual ao original antes de substituir
        if verify_content_before_paste and expected_original_text.strip() and hasattr(self.uia, "try_capture_entire_field"):
            current_field_text, _, _ = self.uia.try_capture_entire_field(original_context.hwnd)
            if current_field_text and current_field_text.strip():
                if compute_text_hash(current_field_text) != compute_text_hash(expected_original_text):
                    return ReplacementResult(
                        success=False,
                        safety_status=ReplacementSafetyStatus.CONTENT_MISMATCH,
                        method_used=CaptureMethod.UI_AUTOMATION,
                        original_text=expected_original_text,
                        new_text=new_text,
                        hwnd=original_context.hwnd,
                        rollback_available=False,
                        message="O conteúdo do campo foi alterado antes da conclusão da tradução; substituição cancelada.",
                        duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                        operation_id=op_id,
                    )

        # Estratégia 1: Substituição direta via Win32 Edit / UI Automation ValuePattern
        if hasattr(self.uia, "try_replace_entire_field"):
            replaced_direct, direct_method = self.uia.try_replace_entire_field(
                original_context.hwnd, new_text, preserve_cursor=True
            )
            if replaced_direct and direct_method is not None:
                self._last_rollback = _RollbackRecord(
                    original_text=expected_original_text,
                    replaced_text=new_text,
                    window_context=original_context,
                    timestamp=time.time(),
                )
                return ReplacementResult(
                    success=True,
                    safety_status=ReplacementSafetyStatus.SAFE,
                    method_used=direct_method,
                    original_text=expected_original_text,
                    new_text=new_text,
                    hwnd=original_context.hwnd,
                    rollback_available=bool(expected_original_text),
                    message="Texto integral substituído pela tradução com sucesso.",
                    duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                    operation_id=op_id,
                )

        # Estratégia 2: Substituição via Ctrl+A + Ctrl+V com validação e restauração do Clipboard
        with self._clipboard_lock:
            previous_clipboard = ""
            try:
                previous_clipboard = self._read_clip()
            except Exception:
                previous_clipboard = ""

            try:
                if check_interrupt_fn is not None and check_interrupt_fn():
                    return ReplacementResult(
                        success=False,
                        safety_status=ReplacementSafetyStatus.TYPING_IN_PROGRESS,
                        method_used=CaptureMethod.CONTROLLED_CLIPBOARD,
                        original_text=expected_original_text,
                        new_text=new_text,
                        hwnd=original_context.hwnd,
                        rollback_available=False,
                        message="Nova interação detectada; substituição abortada.",
                        duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                        operation_id=op_id,
                    )

                if self.inspector.get_foreground_hwnd() != int(original_context.hwnd):
                    return ReplacementResult(
                        success=False,
                        safety_status=ReplacementSafetyStatus.FOCUS_CHANGED,
                        method_used=CaptureMethod.CONTROLLED_CLIPBOARD,
                        original_text=expected_original_text,
                        new_text=new_text,
                        hwnd=original_context.hwnd,
                        rollback_available=False,
                        message="A janela perdeu o foco antes da colagem.",
                        duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                        operation_id=op_id,
                    )

                self._write_clip(new_text)
                time.sleep(0.04 if not self._has_custom_hotkey_fn else 0.005)
                self._send_hotkey("ctrl", "a")
                time.sleep(0.04 if not self._has_custom_hotkey_fn else 0.005)
                self._send_hotkey("ctrl", "v")
                time.sleep(0.25 if not self._has_custom_hotkey_fn else 0.01)

                if previous_clipboard and not previous_clipboard.startswith(self.MARKER_PREFIX):
                    try:
                        self._write_clip(previous_clipboard)
                    except Exception:
                        pass

                self._last_rollback = _RollbackRecord(
                    original_text=expected_original_text,
                    replaced_text=new_text,
                    window_context=original_context,
                    timestamp=time.time(),
                )
                return ReplacementResult(
                    success=True,
                    safety_status=ReplacementSafetyStatus.SAFE,
                    method_used=CaptureMethod.CONTROLLED_CLIPBOARD,
                    original_text=expected_original_text,
                    new_text=new_text,
                    hwnd=original_context.hwnd,
                    rollback_available=bool(expected_original_text),
                    message="Texto integral substituído pela tradução no campo ativo.",
                    duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                    operation_id=op_id,
                )
            except Exception as exc:
                return ReplacementResult(
                    success=False,
                    safety_status=ReplacementSafetyStatus.WINDOW_INVALID,
                    method_used=CaptureMethod.CONTROLLED_CLIPBOARD,
                    original_text=expected_original_text,
                    new_text=new_text,
                    hwnd=original_context.hwnd,
                    rollback_available=False,
                    message=f"Erro ao substituir campo: {exc}",
                    duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                    operation_id=op_id,
                )

    def capture_copied_clipboard(self, target_hwnd: int | None = None) -> CaptureResult:
        """Lê diretamente o texto recém-copiado pelo usuário via Ctrl+C e vincula ao HWND da janela ativa."""
        op_id = generate_operation_id("clip")
        started = time.perf_counter()

        hwnd = target_hwnd or self.inspector.get_foreground_hwnd()
        if not hwnd or not self.inspector.is_window_valid(int(hwnd)):
            return CaptureResult(
                success=False,
                text="",
                window_context=None,
                method=CaptureMethod.CONTROLLED_CLIPBOARD,
                error_message="Janela de origem não disponível ao ler Ctrl+C.",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                operation_id=op_id,
            )

        context = self.inspector.get_window_context_for_hwnd(int(hwnd))
        if context is None or self.inspector.is_own_process(context):
            return CaptureResult(
                success=False,
                text="",
                window_context=context,
                method=CaptureMethod.CONTROLLED_CLIPBOARD,
                error_message="Janela ignorada (próprio tradutor ou inválida).",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                operation_id=op_id,
            )

        with self._clipboard_lock:
            copied_text = self._read_clip()

        if (
            not copied_text
            or not copied_text.strip()
            or copied_text.startswith(self.MARKER_PREFIX)
        ):
            return CaptureResult(
                success=False,
                text="",
                window_context=context,
                method=CaptureMethod.CONTROLLED_CLIPBOARD,
                error_message="A área de transferência está vazia.",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                operation_id=op_id,
            )

        context.selection_hash = compute_text_hash(copied_text)
        duration_ms = (time.perf_counter() - started) * 1000.0
        self.logger.info(
            LogCategory.CAPTURE,
            f"Texto capturado diretamente do Ctrl+C ({len(copied_text)} chars) de '{context.process_name}'.",
            operation_id=op_id,
        )
        return CaptureResult(
            success=True,
            text=copied_text,
            window_context=context,
            method=CaptureMethod.CONTROLLED_CLIPBOARD,
            duration_ms=round(duration_ms, 2),
            operation_id=op_id,
        )

    def capture_selection(self, target_hwnd: int | None = None) -> CaptureResult:
        """Captura a seleção ativa na janela `target_hwnd` (reativando o foco se necessário)."""
        op_id = generate_operation_id("cap")
        started = time.perf_counter()

        hwnd = target_hwnd or self.inspector.get_foreground_hwnd()
        if not hwnd or not self.inspector.is_window_valid(int(hwnd)):
            return CaptureResult(
                success=False,
                text="",
                window_context=None,
                method=CaptureMethod.CONTROLLED_CLIPBOARD,
                error_message="Nenhuma janela válida de origem foi identificada.",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                operation_id=op_id,
            )

        context = self.inspector.get_window_context_for_hwnd(int(hwnd))
        if context is None:
            return CaptureResult(
                success=False,
                text="",
                window_context=None,
                method=CaptureMethod.CONTROLLED_CLIPBOARD,
                error_message="A janela de origem foi fechada ou tornou-se inacessível.",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                operation_id=op_id,
            )

        if self.inspector.is_own_process(context):
            return CaptureResult(
                success=False,
                text="",
                window_context=context,
                method=CaptureMethod.CONTROLLED_CLIPBOARD,
                error_message="A janela ativa pertence ao próprio Universal Translator.",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                operation_id=op_id,
            )

        if self.inspector.get_foreground_hwnd() != int(hwnd):
            self.inspector.activate_window_safely(context)
            time.sleep(0.06 if not self._has_custom_hotkey_fn else 0.005)

        # Estratégia 1: Acessibilidade / Mensagens Nativas Win32
        direct_text, direct_method = self.uia.try_capture_selection(int(hwnd))
        if direct_text and direct_text.strip() and direct_method is not None:
            context.selection_hash = compute_text_hash(direct_text)
            duration_ms = (time.perf_counter() - started) * 1000.0
            return CaptureResult(
                success=True,
                text=direct_text,
                window_context=context,
                method=direct_method,
                duration_ms=round(duration_ms, 2),
                operation_id=op_id,
            )

        # Estratégia 2: Clipboard Controlada com Marcador Explícito e Retry de Leitura
        with self._clipboard_lock:
            previous_clipboard = ""
            try:
                previous_clipboard = self._read_clip()
            except Exception:
                previous_clipboard = ""

            sentinel = f"{self.MARKER_PREFIX}{op_id}__"
            copied_text = sentinel
            try:
                self._write_clip(sentinel)
                time.sleep(0.06 if not self._has_custom_hotkey_fn else 0.005)
                self._send_hotkey("ctrl", "c")

                deadline = time.monotonic() + (1.4 if not self._has_custom_hotkey_fn else 0.15)

                while time.monotonic() < deadline:
                    current_val = self._read_clip()
                    if current_val and current_val != sentinel:
                        copied_text = current_val
                        break
                    time.sleep(0.035 if not self._has_custom_hotkey_fn else 0.005)

                if copied_text == sentinel or not copied_text.strip():
                    if (
                        previous_clipboard
                        and previous_clipboard.strip()
                        and not previous_clipboard.startswith(self.MARKER_PREFIX)
                    ):
                        copied_text = previous_clipboard
                    else:
                        duration_ms = (time.perf_counter() - started) * 1000.0
                        return CaptureResult(
                            success=False,
                            text="",
                            window_context=context,
                            method=CaptureMethod.CONTROLLED_CLIPBOARD,
                            error_message="Nenhum texto selecionado pôde ser copiado do controle ativo.",
                            duration_ms=round(duration_ms, 2),
                            operation_id=op_id,
                        )

                context.selection_hash = compute_text_hash(copied_text)
                duration_ms = (time.perf_counter() - started) * 1000.0
                self.logger.info(
                    LogCategory.CAPTURE,
                    f"Texto capturado ({len(copied_text)} chars) de '{context.process_name}'.",
                    operation_id=op_id,
                )
                return CaptureResult(
                    success=True,
                    text=copied_text,
                    window_context=context,
                    method=CaptureMethod.CONTROLLED_CLIPBOARD,
                    duration_ms=round(duration_ms, 2),
                    operation_id=op_id,
                )
            except Exception as exc:
                return CaptureResult(
                    success=False,
                    text="",
                    window_context=context,
                    method=CaptureMethod.CONTROLLED_CLIPBOARD,
                    error_message=f"Falha ao capturar seleção: {exc}",
                    duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                    operation_id=op_id,
                )
            finally:
                try:
                    if previous_clipboard and not previous_clipboard.startswith(self.MARKER_PREFIX):
                        self._write_clip(previous_clipboard)
                    elif copied_text and copied_text != sentinel:
                        self._write_clip(copied_text)
                    else:
                        self._write_clip("")
                except Exception:
                    pass

    def replace_text_safely(
        self,
        new_text: str,
        original_context: WindowContext,
        expected_original_text: str = "",
        verify_content_before_paste: bool = False,
    ) -> ReplacementResult:
        op_id = generate_operation_id("rep")
        started = time.perf_counter()

        if not new_text or not new_text.strip():
            return ReplacementResult(
                success=False,
                safety_status=ReplacementSafetyStatus.EMPTY_TRANSLATION,
                method_used=CaptureMethod.CONTROLLED_CLIPBOARD,
                original_text=expected_original_text,
                new_text="",
                hwnd=original_context.hwnd if original_context else None,
                rollback_available=False,
                message="A tradução está vazia; substituição cancelada.",
                duration_ms=0.0,
                operation_id=op_id,
            )

        if original_context is None or not self.inspector.is_window_valid(original_context.hwnd):
            return ReplacementResult(
                success=False,
                safety_status=ReplacementSafetyStatus.WINDOW_INVALID,
                method_used=CaptureMethod.CONTROLLED_CLIPBOARD,
                original_text=expected_original_text,
                new_text=new_text,
                hwnd=original_context.hwnd if original_context else None,
                rollback_available=False,
                message="A janela original não está mais disponível.",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        if not self.inspector.activate_window_safely(original_context):
            return ReplacementResult(
                success=False,
                safety_status=ReplacementSafetyStatus.FOCUS_CHANGED,
                method_used=CaptureMethod.CONTROLLED_CLIPBOARD,
                original_text=expected_original_text,
                new_text=new_text,
                hwnd=original_context.hwnd,
                rollback_available=False,
                message="Não foi possível retornar ao aplicativo original.",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        if verify_content_before_paste and expected_original_text.strip():
            direct_current, _ = self.uia.try_capture_selection(original_context.hwnd)
            if direct_current and direct_current.strip():
                if compute_text_hash(direct_current) != compute_text_hash(expected_original_text):
                    return ReplacementResult(
                        success=False,
                        safety_status=ReplacementSafetyStatus.CONTENT_MISMATCH,
                        method_used=CaptureMethod.WIN32_MESSAGE,
                        original_text=expected_original_text,
                        new_text=new_text,
                        hwnd=original_context.hwnd,
                        rollback_available=False,
                        message="O conteúdo selecionado mudou desde a captura.",
                        duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                        operation_id=op_id,
                    )

        # Estratégia 1: Controles nativos Win32 Edit/RichEdit
        replaced_direct, direct_method = self.uia.try_replace_selection(
            original_context.hwnd, new_text
        )
        if replaced_direct and direct_method is not None:
            self._last_rollback = _RollbackRecord(
                original_text=expected_original_text,
                replaced_text=new_text,
                window_context=original_context,
                timestamp=time.time(),
            )
            return ReplacementResult(
                success=True,
                safety_status=ReplacementSafetyStatus.SAFE,
                method_used=direct_method,
                original_text=expected_original_text,
                new_text=new_text,
                hwnd=original_context.hwnd,
                rollback_available=bool(expected_original_text),
                message="Tradução inserida no campo original.",
                duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                operation_id=op_id,
            )

        # Estratégia 2: Colagem via Clipboard Controlada
        with self._clipboard_lock:
            previous_clipboard = ""
            try:
                previous_clipboard = self._read_clip()
            except Exception:
                previous_clipboard = ""

            should_restore_previous = (
                bool(previous_clipboard)
                and not previous_clipboard.startswith(self.MARKER_PREFIX)
                and previous_clipboard.strip() != (expected_original_text or "").strip()
            )

            try:
                current_fg = self.inspector.get_foreground_hwnd()
                if current_fg is not None and current_fg != int(original_context.hwnd):
                    return ReplacementResult(
                        success=False,
                        safety_status=ReplacementSafetyStatus.FOCUS_CHANGED,
                        method_used=CaptureMethod.CONTROLLED_CLIPBOARD,
                        original_text=expected_original_text,
                        new_text=new_text,
                        hwnd=original_context.hwnd,
                        rollback_available=False,
                        message="O foco mudou para outro aplicativo antes da colagem.",
                        duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                        operation_id=op_id,
                    )

                self._write_clip(new_text)
                time.sleep(0.10 if not self._has_custom_hotkey_fn else 0.005)
                self._send_hotkey("ctrl", "v")
                time.sleep(0.65 if not self._has_custom_hotkey_fn else 0.01)

                if should_restore_previous:
                    try:
                        self._write_clip(previous_clipboard)
                    except Exception:
                        pass

                self._last_rollback = _RollbackRecord(
                    original_text=expected_original_text,
                    replaced_text=new_text,
                    window_context=original_context,
                    timestamp=time.time(),
                )
                duration_ms = (time.perf_counter() - started) * 1000.0
                self.logger.info(
                    LogCategory.CLIPBOARD,
                    "Tradução colada com sucesso no campo original.",
                    operation_id=op_id,
                )
                return ReplacementResult(
                    success=True,
                    safety_status=ReplacementSafetyStatus.SAFE,
                    method_used=CaptureMethod.CONTROLLED_CLIPBOARD,
                    original_text=expected_original_text,
                    new_text=new_text,
                    hwnd=original_context.hwnd,
                    rollback_available=bool(expected_original_text),
                    message="Tradução inserida no campo original.",
                    duration_ms=round(duration_ms, 2),
                    operation_id=op_id,
                )
            except Exception as exc:
                return ReplacementResult(
                    success=False,
                    safety_status=ReplacementSafetyStatus.WINDOW_INVALID,
                    method_used=CaptureMethod.CONTROLLED_CLIPBOARD,
                    original_text=expected_original_text,
                    new_text=new_text,
                    hwnd=original_context.hwnd,
                    rollback_available=False,
                    message=f"Falha ao substituir o texto: {exc}",
                    duration_ms=round((time.perf_counter() - started) * 1000.0, 2),
                    operation_id=op_id,
                )

    def rollback_last_replacement(self) -> ReplacementResult:
        if self._last_rollback is None or not self._last_rollback.original_text:
            return ReplacementResult(
                success=False,
                safety_status=ReplacementSafetyStatus.EMPTY_TRANSLATION,
                method_used=CaptureMethod.CONTROLLED_CLIPBOARD,
                original_text="",
                new_text="",
                hwnd=None,
                rollback_available=False,
                message="Não há substituição recente disponível para desfazer.",
            )

        record = self._last_rollback
        self._last_rollback = None
        return self.replace_text_safely(
            new_text=record.original_text,
            original_context=record.window_context,
            expected_original_text=record.replaced_text,
            verify_content_before_paste=False,
        )
