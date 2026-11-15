"""Adaptador de Acessibilidade e Controles Nativos (Windows UI Automation & Win32 Edit Messages)."""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from ...domain.enums import CaptureMethod, ControlAccessibilityKind, LogCategory
from ..logging.logger import ObservabilityLogger
from ..windows.win32_inspector import Win32WindowInspector

WM_GETTEXTLENGTH = 0x000E
WM_GETTEXT = 0x000D
WM_SETTEXT = 0x000C
EM_GETSEL = 0x00B0
EM_SETSEL = 0x00B1
EM_REPLACESEL = 0x00C2

_NATIVE_EDIT_CLASSES = {"edit", "richedit20w", "richedit50w", "richeditd2dpt", "scintilla"}
_BROWSER_AND_APP_CLASSES = {
    "chrome_widgetwin_1",
    "mozillawindowclass",
    "applicationframeinputsinkwindow",
    " intermediate d3d window",
}
_SENSITIVE_CONTROL_KEYWORDS = (
    "password",
    "senha",
    "passcode",
    "pin",
    "cvv",
    "credit card",
    "cartão",
    "api key",
    "apikey",
    "secret",
    "token",
    "2fa",
    "otp",
)


class UIAutomationAdapter:
    """Captura e substitui texto diretamente via Win32 Edit Messages ou Windows UI Automation.

    Diferencia rigorosamente:
    - Campos editáveis (`ControlAccessibilityKind.EDITABLE_FIELD`)
    - Textos selecionáveis somente leitura (`ControlAccessibilityKind.SELECTABLE_TEXT`)
    - Elementos acessíveis não editáveis (`ControlAccessibilityKind.ACCESSIBLE_ELEMENT`)
    - Campos de senha ou sensíveis (`ControlAccessibilityKind.SENSITIVE_OR_PASSWORD`)
    - Superfícies sem suporte de acessibilidade textual que exigiriam OCR (`ControlAccessibilityKind.REQUIRES_OCR_OR_UNACCESSIBLE`)
    """

    def __init__(
        self,
        window_inspector: Win32WindowInspector,
        logger: ObservabilityLogger | None = None,
    ) -> None:
        self._inspector = window_inspector
        self._logger = logger or ObservabilityLogger.get_instance()
        self._is_win32 = sys.platform == "win32"
        self._uia_mod = self._load_uiautomation()

    @staticmethod
    def _load_uiautomation():
        try:
            import uiautomation as auto  # type: ignore
            return auto
        except Exception:
            return None

    def classify_focused_control(self, target_hwnd: int) -> ControlAccessibilityKind:
        """Classifica o elemento atualmente focado na janela alvo."""
        if not self._is_win32 or not target_hwnd:
            return ControlAccessibilityKind.REQUIRES_OCR_OR_UNACCESSIBLE

        ctx = self._inspector.get_window_context_for_hwnd(target_hwnd)
        if ctx and self._inspector.is_sensitive_window_context(ctx):
            return ControlAccessibilityKind.SENSITIVE_OR_PASSWORD

        focus_hwnd = self._inspector.get_focused_child_hwnd(target_hwnd) or target_hwnd
        if self._inspector.is_password_style_hwnd(focus_hwnd):
            return ControlAccessibilityKind.SENSITIVE_OR_PASSWORD

        # 1. Inspeção via UI Automation quando disponível
        if self._uia_mod is not None:
            try:
                focused = self._uia_mod.GetFocusedControl()
                if focused is not None:
                    if bool(getattr(focused, "IsPassword", False)):
                        return ControlAccessibilityKind.SENSITIVE_OR_PASSWORD

                    name_lower = str(getattr(focused, "Name", "") or "").lower()
                    auto_id_lower = str(getattr(focused, "AutomationId", "") or "").lower()
                    combined_meta = f"{name_lower} {auto_id_lower}"
                    if any(kw in combined_meta for kw in _SENSITIVE_CONTROL_KEYWORDS):
                        return ControlAccessibilityKind.SENSITIVE_OR_PASSWORD

                    ctrl_type = str(getattr(focused, "ControlTypeName", "") or "").lower()
                    is_enabled = bool(getattr(focused, "IsEnabled", True))
                    is_focusable = bool(getattr(focused, "IsKeyboardFocusable", True))

                    # Verifica ValuePattern para distinguir editável vs somente leitura
                    is_readonly = False
                    has_value_pattern = False
                    try:
                        val_pat = focused.GetValuePattern()
                        if val_pat is not None:
                            has_value_pattern = True
                            is_readonly = bool(getattr(val_pat, "IsReadOnly", False))
                    except Exception:
                        pass

                    if ctrl_type in ("edit", "editcontrol", "combobox", "comboboxcontrol"):
                        if not is_enabled or is_readonly:
                            return ControlAccessibilityKind.SELECTABLE_TEXT
                        return ControlAccessibilityKind.EDITABLE_FIELD

                    if ctrl_type in ("document", "documentcontrol", "custom", "customcontrol", "group", "pane"):
                        if has_value_pattern and not is_readonly and is_enabled and is_focusable:
                            return ControlAccessibilityKind.EDITABLE_FIELD
                        # Elementos com TextPattern sem edição são textos selecionáveis
                        try:
                            txt_pat = focused.GetTextPattern()
                            if txt_pat is not None:
                                return (
                                    ControlAccessibilityKind.EDITABLE_FIELD
                                    if (has_value_pattern and not is_readonly)
                                    else ControlAccessibilityKind.SELECTABLE_TEXT
                                )
                        except Exception:
                            pass

                    if ctrl_type in ("text", "textcontrol"):
                        return ControlAccessibilityKind.SELECTABLE_TEXT

                    if ctrl_type in ("button", "buttoncontrol", "menuitem", "listitem", "treeitem"):
                        return ControlAccessibilityKind.ACCESSIBLE_ELEMENT
            except Exception:
                pass

        # 2. Inspeção via classes Win32 nativas
        try:
            user32 = self._inspector.user32
            if user32:
                cls_buf = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(wintypes.HWND(focus_hwnd), cls_buf, 128)
                cls_name = (cls_buf.value or "").lower()
                if any(name in cls_name for name in _NATIVE_EDIT_CLASSES):
                    if self._inspector.is_readonly_style_hwnd(focus_hwnd):
                        return ControlAccessibilityKind.SELECTABLE_TEXT
                    return ControlAccessibilityKind.EDITABLE_FIELD
                if any(name in cls_name for name in _BROWSER_AND_APP_CLASSES) and self._uia_mod is None:
                    # Quando uiautomation não está instalado, permite apps web/chat conhecidos se não forem sensíveis
                    return ControlAccessibilityKind.EDITABLE_FIELD
        except Exception:
            pass

        return ControlAccessibilityKind.REQUIRES_OCR_OR_UNACCESSIBLE

    def is_focused_editable_control(self, target_hwnd: int) -> bool:
        """Fail-closed: só autoriza captura integral/substituição em controles comprovadamente editáveis e não sensíveis."""
        return self.classify_focused_control(target_hwnd) == ControlAccessibilityKind.EDITABLE_FIELD

    def is_password_or_sensitive_control(self, target_hwnd: int) -> bool:
        """Retorna True se o controle focado for um campo de senha ou dado sensível."""
        return self.classify_focused_control(target_hwnd) == ControlAccessibilityKind.SENSITIVE_OR_PASSWORD

    def get_focused_control_identifier(self, target_hwnd: int) -> tuple[int, str]:
        """Retorna (focus_hwnd, unique_control_id) para garantir que a tradução nunca substitua outro campo."""
        focus_hwnd = (
            self._inspector.get_focused_child_hwnd(target_hwnd) or target_hwnd
            if self._is_win32
            else target_hwnd
        )
        control_id = f"hwnd:{focus_hwnd}"
        if self._uia_mod is not None:
            try:
                focused = self._uia_mod.GetFocusedControl()
                if focused is not None:
                    runtime_id = getattr(focused, "GetRuntimeId", lambda: None)()
                    auto_id = str(getattr(focused, "AutomationId", "") or "")
                    ctrl_type = str(getattr(focused, "ControlTypeName", "") or "")
                    if runtime_id:
                        control_id = f"uia:{tuple(runtime_id)}:{auto_id}:{ctrl_type}"
                    elif auto_id:
                        control_id = f"hwnd:{focus_hwnd}:auto:{auto_id}:{ctrl_type}"
            except Exception:
                pass
        return int(focus_hwnd), control_id

    def get_cursor_selection_range(self, target_hwnd: int) -> tuple[int, int] | None:
        """Obtém o intervalo atual do cursor/seleção (start, end) em controles nativos Win32."""
        if not self._is_win32 or not self._inspector.user32:
            return None
        try:
            focus_hwnd = self._inspector.get_focused_child_hwnd(target_hwnd) or target_hwnd
            cls_buf = ctypes.create_unicode_buffer(128)
            self._inspector.user32.GetClassNameW(wintypes.HWND(focus_hwnd), cls_buf, 128)
            cls_name = (cls_buf.value or "").lower()
            if not any(c in cls_name for c in _NATIVE_EDIT_CLASSES):
                return None
            start_pos = wintypes.DWORD(0)
            end_pos = wintypes.DWORD(0)
            self._inspector.user32.SendMessageW(
                wintypes.HWND(focus_hwnd),
                EM_GETSEL,
                ctypes.byref(start_pos),
                ctypes.byref(end_pos),
            )
            return int(start_pos.value), int(end_pos.value)
        except Exception:
            return None

    def try_capture_entire_field(
        self, target_hwnd: int
    ) -> tuple[str, CaptureMethod | None, tuple[int, int] | None]:
        """Captura todo o conteúdo de um campo editável via Win32 Message ou UI Automation sem usar Clipboard."""
        if not self._is_win32:
            return "", None, None

        if self.is_password_or_sensitive_control(target_hwnd):
            return "", None, None

        # 1. Controles nativos Win32 Edit / RichEdit (WM_GETTEXT integral)
        text, cursor_range = self._try_win32_edit_get_full_text(target_hwnd)
        if text:
            return text, CaptureMethod.WIN32_MESSAGE, cursor_range

        # 2. Windows UI Automation (ValuePattern / TextPattern.DocumentRange)
        uia_text = self._try_uia_get_full_text()
        if uia_text:
            return uia_text, CaptureMethod.UI_AUTOMATION, None

        return "", None, None

    def try_replace_entire_field(
        self,
        target_hwnd: int,
        new_text: str,
        preserve_cursor: bool = True,
    ) -> tuple[bool, CaptureMethod | None]:
        """Substitui o texto integral do campo via APIs nativas/acessibilidade preservando cursor e Undo."""
        if not self._is_win32 or not new_text:
            return False, None

        if self.is_password_or_sensitive_control(target_hwnd):
            return False, None

        if self._try_win32_edit_replace_full_text(target_hwnd, new_text, preserve_cursor=preserve_cursor):
            return True, CaptureMethod.WIN32_MESSAGE

        if self._try_uia_set_full_text(new_text):
            return True, CaptureMethod.UI_AUTOMATION

        return False, None

    def try_capture_selection(self, target_hwnd: int) -> tuple[str, CaptureMethod | None]:
        """Tenta capturar o texto selecionado via Win32 Message ou UI Automation antes de recorrer ao Clipboard."""
        if not self._is_win32:
            return "", None

        if self.is_password_or_sensitive_control(target_hwnd):
            return "", None

        text = self._try_win32_edit_get_selection(target_hwnd)
        if text:
            return text, CaptureMethod.WIN32_MESSAGE

        text = self._try_uia_get_selection()
        if text:
            return text, CaptureMethod.UI_AUTOMATION

        return "", None

    def try_replace_selection(self, target_hwnd: int, new_text: str) -> tuple[bool, CaptureMethod | None]:
        """Tenta substituir a seleção diretamente mantendo suporte nativo a Ctrl+Z (Undo)."""
        if not self._is_win32 or not new_text:
            return False, None

        if self.is_password_or_sensitive_control(target_hwnd):
            return False, None

        if self._try_win32_edit_replace_selection(target_hwnd, new_text):
            return True, CaptureMethod.WIN32_MESSAGE

        return False, None

    def _try_win32_edit_get_full_text(
        self, target_hwnd: int
    ) -> tuple[str, tuple[int, int] | None]:
        user32 = self._inspector.user32
        if not user32:
            return "", None
        try:
            focus_hwnd = self._inspector.get_focused_child_hwnd(target_hwnd) or target_hwnd
            cls_buf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(wintypes.HWND(focus_hwnd), cls_buf, 128)
            cls_name = (cls_buf.value or "").lower()
            if not any(c in cls_name for c in _NATIVE_EDIT_CLASSES):
                return "", None
            if self._inspector.is_password_style_hwnd(focus_hwnd):
                return "", None

            start_pos = wintypes.DWORD(0)
            end_pos = wintypes.DWORD(0)
            user32.SendMessageW(
                wintypes.HWND(focus_hwnd),
                EM_GETSEL,
                ctypes.byref(start_pos),
                ctypes.byref(end_pos),
            )
            cursor_range = (int(start_pos.value), int(end_pos.value))

            total_len = int(
                user32.SendMessageW(wintypes.HWND(focus_hwnd), WM_GETTEXTLENGTH, 0, 0)
            )
            if total_len <= 0 or total_len > 250_000:
                return "", cursor_range

            buf = ctypes.create_unicode_buffer(total_len + 2)
            user32.SendMessageW(
                wintypes.HWND(focus_hwnd),
                WM_GETTEXT,
                total_len + 1,
                ctypes.cast(buf, ctypes.c_void_p).value,
            )
            full_text = buf.value
            return (full_text if full_text.strip() else ""), cursor_range
        except Exception as exc:
            self._logger.warning(
                LogCategory.ACCESSIBILITY,
                f"Fallback de leitura integral Win32 Edit acionado: {exc}",
            )
            return "", None

    def _try_win32_edit_replace_full_text(
        self,
        target_hwnd: int,
        new_text: str,
        preserve_cursor: bool = True,
    ) -> bool:
        user32 = self._inspector.user32
        if not user32:
            return False
        try:
            focus_hwnd = self._inspector.get_focused_child_hwnd(target_hwnd) or target_hwnd
            cls_buf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(wintypes.HWND(focus_hwnd), cls_buf, 128)
            cls_name = (cls_buf.value or "").lower()
            if not any(c in cls_name for c in _NATIVE_EDIT_CLASSES):
                return False
            if self._inspector.is_password_style_hwnd(focus_hwnd) or self._inspector.is_readonly_style_hwnd(focus_hwnd):
                return False

            # Seleciona todo o conteúdo do controle (0 a -1) e substitui com Undo habilitado (wParam=1)
            user32.SendMessageW(wintypes.HWND(focus_hwnd), EM_SETSEL, 0, -1)
            buf = ctypes.create_unicode_buffer(new_text)
            user32.SendMessageW(
                wintypes.HWND(focus_hwnd),
                EM_REPLACESEL,
                1,
                ctypes.cast(buf, ctypes.c_void_p).value,
            )
            if preserve_cursor:
                end_idx = len(new_text)
                user32.SendMessageW(wintypes.HWND(focus_hwnd), EM_SETSEL, end_idx, end_idx)
            return True
        except Exception:
            return False

    def _try_win32_edit_get_selection(self, target_hwnd: int) -> str:
        user32 = self._inspector.user32
        if not user32:
            return ""
        try:
            focus_hwnd = self._inspector.get_focused_child_hwnd(target_hwnd) or target_hwnd
            cls_buf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(wintypes.HWND(focus_hwnd), cls_buf, 128)
            cls_name = (cls_buf.value or "").lower()
            if not any(c in cls_name for c in _NATIVE_EDIT_CLASSES):
                return ""
            if self._inspector.is_password_style_hwnd(focus_hwnd):
                return ""

            start_pos = wintypes.DWORD(0)
            end_pos = wintypes.DWORD(0)
            user32.SendMessageW(
                wintypes.HWND(focus_hwnd),
                EM_GETSEL,
                ctypes.byref(start_pos),
                ctypes.byref(end_pos),
            )
            s_idx = int(start_pos.value)
            e_idx = int(end_pos.value)
            if e_idx <= s_idx:
                return ""

            total_len = int(
                user32.SendMessageW(wintypes.HWND(focus_hwnd), WM_GETTEXTLENGTH, 0, 0)
            )
            if total_len <= 0 or total_len > 250_000:
                return ""

            buf = ctypes.create_unicode_buffer(total_len + 2)
            user32.SendMessageW(
                wintypes.HWND(focus_hwnd),
                WM_GETTEXT,
                total_len + 1,
                ctypes.cast(buf, ctypes.c_void_p).value,
            )
            full_text = buf.value
            selected = full_text[s_idx:e_idx]
            return selected if selected.strip() else ""
        except Exception as exc:
            self._logger.warning(
                LogCategory.ACCESSIBILITY,
                f"Fallback de Win32 Edit acionado: {exc}",
            )
            return ""

    def _try_win32_edit_replace_selection(self, target_hwnd: int, new_text: str) -> bool:
        user32 = self._inspector.user32
        if not user32:
            return False
        try:
            focus_hwnd = self._inspector.get_focused_child_hwnd(target_hwnd) or target_hwnd
            cls_buf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(wintypes.HWND(focus_hwnd), cls_buf, 128)
            cls_name = (cls_buf.value or "").lower()
            if not any(c in cls_name for c in _NATIVE_EDIT_CLASSES):
                return False
            if self._inspector.is_password_style_hwnd(focus_hwnd) or self._inspector.is_readonly_style_hwnd(focus_hwnd):
                return False

            buf = ctypes.create_unicode_buffer(new_text)
            user32.SendMessageW(
                wintypes.HWND(focus_hwnd),
                EM_REPLACESEL,
                1,
                ctypes.cast(buf, ctypes.c_void_p).value,
            )
            return True
        except Exception:
            return False

    def _try_uia_get_full_text(self) -> str:
        if self._uia_mod is None:
            return ""
        try:
            focused = self._uia_mod.GetFocusedControl()
            if not focused or bool(getattr(focused, "IsPassword", False)):
                return ""
            try:
                val_pat = focused.GetValuePattern()
                if val_pat is not None and not bool(getattr(val_pat, "IsReadOnly", False)):
                    val = getattr(val_pat, "Value", "")
                    if val and str(val).strip():
                        return str(val)
            except Exception:
                pass
            try:
                txt_pat = focused.GetTextPattern()
                if txt_pat is not None and hasattr(txt_pat, "DocumentRange"):
                    doc_range = txt_pat.DocumentRange
                    if doc_range is not None:
                        doc_text = doc_range.GetText(-1)
                        if doc_text and str(doc_text).strip():
                            return str(doc_text)
            except Exception:
                pass
        except Exception:
            pass
        return ""

    def _try_uia_set_full_text(self, new_text: str) -> bool:
        if self._uia_mod is None:
            return False
        try:
            focused = self._uia_mod.GetFocusedControl()
            if not focused or bool(getattr(focused, "IsPassword", False)):
                return False
            val_pat = focused.GetValuePattern()
            if val_pat is not None and not bool(getattr(val_pat, "IsReadOnly", False)):
                val_pat.SetValue(new_text)
                return True
        except Exception:
            pass
        return False

    def _try_uia_get_selection(self) -> str:
        if self._uia_mod is None:
            return ""
        try:
            focused = self._uia_mod.GetFocusedControl()
            if not focused or bool(getattr(focused, "IsPassword", False)):
                return ""
            text_pattern = focused.GetTextPattern()
            if text_pattern:
                selections = text_pattern.GetSelection()
                if selections and len(selections) > 0:
                    selected_text = selections[0].GetText(-1)
                    if selected_text and selected_text.strip():
                        return str(selected_text)
        except Exception:
            pass
        return ""
