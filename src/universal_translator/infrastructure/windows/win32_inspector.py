"""Inspeção de janelas e gerenciamento seguro de foco via Windows API (user32 / kernel32)."""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

from ...domain.contracts import WindowInspectorPort
from ...domain.entities import WindowContext
from ...domain.enums import LogCategory
from ..logging.logger import ObservabilityLogger

SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
ES_PASSWORD = 0x0020
ES_READONLY = 0x0800

_SENSITIVE_PROCESSES = {
    "keepass.exe",
    "keepassxc.exe",
    "1password.exe",
    "bitwarden.exe",
    "lastpass.exe",
    "credentialuibroker.exe",
    "consent.exe",
    "lsass.exe",
}

_SENSITIVE_TITLE_KEYWORDS = (
    "password",
    "senha",
    "credencial",
    "credential",
    "bitwarden",
    "1password",
    "keepass",
    "autenticação de dois fatores",
    "two-factor",
    "2fa",
    "pin de segurança",
)


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", _RECT),
    ]


class Win32WindowInspector(WindowInspectorPort):
    """Subsistema Win32 para inspeção de janela, processo, foco, estilo de senha e preservação de maximização."""

    def __init__(self, logger: ObservabilityLogger | None = None) -> None:
        self._logger = logger or ObservabilityLogger.get_instance()
        self._is_win32 = sys.platform == "win32"
        self.user32 = None
        self.kernel32 = None
        if self._is_win32:
            self._bind_win32_apis()

    def _bind_win32_apis(self) -> None:
        try:
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            self.user32.GetForegroundWindow.argtypes = []
            self.user32.GetForegroundWindow.restype = wintypes.HWND

            self.user32.IsWindow.argtypes = [wintypes.HWND]
            self.user32.IsWindow.restype = wintypes.BOOL

            self.user32.IsIconic.argtypes = [wintypes.HWND]
            self.user32.IsIconic.restype = wintypes.BOOL

            self.user32.IsZoomed.argtypes = [wintypes.HWND]
            self.user32.IsZoomed.restype = wintypes.BOOL

            self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            self.user32.GetWindowTextLengthW.restype = ctypes.c_int

            self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            self.user32.GetWindowTextW.restype = ctypes.c_int

            self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            self.user32.GetClassNameW.restype = ctypes.c_int

            self.user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(wintypes.DWORD),
            ]
            self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD

            self.kernel32.GetCurrentThreadId.argtypes = []
            self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD

            self.user32.AttachThreadInput.argtypes = [
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.BOOL,
            ]
            self.user32.AttachThreadInput.restype = wintypes.BOOL

            self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            self.user32.ShowWindow.restype = wintypes.BOOL

            self.user32.BringWindowToTop.argtypes = [wintypes.HWND]
            self.user32.BringWindowToTop.restype = wintypes.BOOL

            self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            self.user32.SetForegroundWindow.restype = wintypes.BOOL

            self.user32.GetClipboardSequenceNumber.argtypes = []
            self.user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

            self.user32.GetGUIThreadInfo.argtypes = [
                wintypes.DWORD,
                ctypes.POINTER(_GUITHREADINFO),
            ]
            self.user32.GetGUIThreadInfo.restype = wintypes.BOOL
        except Exception as exc:
            self._logger.error(
                LogCategory.WINDOWS_INTEGRATION,
                f"Falha ao vincular APIs Win32: {exc}",
            )

    def get_foreground_hwnd(self) -> int | None:
        if not self._is_win32 or not self.user32:
            return None
        hwnd = self.user32.GetForegroundWindow()
        return int(hwnd) if hwnd else None

    def is_window_valid(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        if not self._is_win32 or not self.user32:
            return False
        return bool(self.user32.IsWindow(wintypes.HWND(int(hwnd))))

    def get_focused_child_hwnd(self, target_hwnd: int) -> int | None:
        """Retorna o controle filho focado (hwndFocus) dentro da thread da janela alvo."""
        if not self._is_win32 or not self.user32 or not target_hwnd:
            return None
        try:
            pid = wintypes.DWORD(0)
            tid = self.user32.GetWindowThreadProcessId(
                wintypes.HWND(int(target_hwnd)), ctypes.byref(pid)
            )
            if not tid:
                return int(target_hwnd)
            gti = _GUITHREADINFO()
            gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
            if self.user32.GetGUIThreadInfo(tid, ctypes.byref(gti)) and gti.hwndFocus:
                return int(gti.hwndFocus)
        except Exception:
            pass
        return int(target_hwnd)

    def get_window_style(self, hwnd: int) -> int:
        if not self._is_win32 or not self.user32 or not hwnd:
            return 0
        try:
            get_long = getattr(self.user32, "GetWindowLongPtrW", self.user32.GetWindowLongW)
            get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
            get_long.restype = ctypes.c_ssize_t
            return int(get_long(int(hwnd), GWL_STYLE))
        except Exception:
            return 0

    def is_password_style_hwnd(self, hwnd: int) -> bool:
        """Verifica se o controle nativo Win32 possui a flag ES_PASSWORD."""
        style = self.get_window_style(hwnd)
        return bool(style & ES_PASSWORD)

    def is_readonly_style_hwnd(self, hwnd: int) -> bool:
        """Verifica se o controle nativo Win32 possui a flag ES_READONLY."""
        style = self.get_window_style(hwnd)
        return bool(style & ES_READONLY)

    @staticmethod
    def is_sensitive_window_context(context: WindowContext | None) -> bool:
        """Detecta janelas de cofres de senha, prompts UAC/credenciais ou campos sensíveis."""
        if context is None:
            return False
        if context.is_password_or_sensitive:
            return True
        proc = (context.process_name or "").strip().lower()
        if proc in _SENSITIVE_PROCESSES:
            return True
        title = (context.window_title or "").strip().lower()
        return any(kw in title for kw in _SENSITIVE_TITLE_KEYWORDS)

    def get_window_context_for_hwnd(self, hwnd: int) -> WindowContext | None:
        if not self.is_window_valid(hwnd):
            return None
        assert self.user32 is not None and self.kernel32 is not None

        h = wintypes.HWND(int(hwnd))
        length = int(self.user32.GetWindowTextLengthW(h))
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 2)
            self.user32.GetWindowTextW(h, buf, length + 2)
            title = buf.value

        cls_buf = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(h, cls_buf, 256)
        class_name = cls_buf.value

        pid_dword = wintypes.DWORD(0)
        tid = int(self.user32.GetWindowThreadProcessId(h, ctypes.byref(pid_dword)))
        pid = int(pid_dword.value)
        proc_name = self._query_process_name(pid)

        is_minimized = bool(self.user32.IsIconic(h))
        is_maximized = bool(self.user32.IsZoomed(h))
        focus_child = self.get_focused_child_hwnd(int(hwnd)) or int(hwnd)
        is_pwd = self.is_password_style_hwnd(focus_child)

        ctx = WindowContext(
            hwnd=int(hwnd),
            process_id=pid,
            process_name=proc_name,
            window_title=title,
            class_name=class_name,
            thread_id=tid,
            is_maximized=is_maximized,
            is_minimized=is_minimized,
            is_elevated=False,
            focused_control_hwnd=int(focus_child),
            focused_control_id=f"hwnd:{focus_child}",
            is_password_or_sensitive=is_pwd,
        )
        if self.is_sensitive_window_context(ctx):
            ctx.is_password_or_sensitive = True
        return ctx

    def get_foreground_window_context(self) -> WindowContext | None:
        hwnd = self.get_foreground_hwnd()
        if not hwnd:
            return None
        return self.get_window_context_for_hwnd(hwnd)

    def _query_process_name(self, pid: int) -> str:
        if not pid or not self._is_win32 or not self.kernel32:
            return ""
        try:
            self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel32.OpenProcess.restype = wintypes.HANDLE
            self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel32.CloseHandle.restype = wintypes.BOOL
            self.kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

            hproc = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not hproc:
                return ""
            try:
                size = wintypes.DWORD(1024)
                buf = ctypes.create_unicode_buffer(1024)
                if self.kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                    return Path(buf.value).name
            finally:
                self.kernel32.CloseHandle(hproc)
        except Exception:
            pass
        return ""

    def activate_window_safely(self, context: WindowContext) -> bool:
        """Restaura o foco à janela de origem sem desmaximizar janelas maximizadas."""
        hwnd = int(context.hwnd)
        if not self.is_window_valid(hwnd):
            return False
        assert self.user32 is not None and self.kernel32 is not None

        if self.get_foreground_hwnd() == hwnd:
            return True

        current_thread = int(self.kernel32.GetCurrentThreadId())
        target_thread = int(
            self.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
        )
        attached = False

        try:
            if target_thread and target_thread != current_thread:
                attached = bool(
                    self.user32.AttachThreadInput(current_thread, target_thread, True)
                )

            if self.user32.IsIconic(wintypes.HWND(hwnd)):
                self.user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)

            self.user32.BringWindowToTop(wintypes.HWND(hwnd))
            self.user32.SetForegroundWindow(wintypes.HWND(hwnd))

            deadline = time.monotonic() + 0.35
            while time.monotonic() < deadline:
                if self.get_foreground_hwnd() == hwnd:
                    return True
                time.sleep(0.025)

            return self.get_foreground_hwnd() == hwnd
        finally:
            if attached:
                self.user32.AttachThreadInput(current_thread, target_thread, False)

    def get_clipboard_sequence_number(self) -> int:
        if not self._is_win32 or not self.user32:
            return 0
        try:
            return int(self.user32.GetClipboardSequenceNumber())
        except Exception:
            return 0

    def apply_no_activate_style(self, hwnd: int) -> bool:
        """Garante via WS_EX_NOACTIVATE que o FloatingButton jamais roube foco da janela de chat."""
        if not self._is_win32 or not self.user32 or not hwnd:
            return False
        try:
            get_long = getattr(self.user32, "GetWindowLongPtrW", self.user32.GetWindowLongW)
            set_long = getattr(self.user32, "SetWindowLongPtrW", self.user32.SetWindowLongW)
            get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
            get_long.restype = ctypes.c_ssize_t
            set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            set_long.restype = ctypes.c_ssize_t

            current = get_long(hwnd, GWL_EXSTYLE)
            set_long(hwnd, GWL_EXSTYLE, current | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            self.user32.SetWindowPos(
                ctypes.c_void_p(hwnd),
                ctypes.c_void_p(-1),
                0,
                0,
                0,
                0,
                0x0001 | 0x0002 | 0x0010 | 0x0020,
            )
            return True
        except Exception:
            return False

    @staticmethod
    def is_own_process(context: WindowContext | None) -> bool:
        if context is None or not context.process_id:
            return False
        return int(context.process_id) == os.getpid()
