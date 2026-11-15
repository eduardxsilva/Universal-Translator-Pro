"""Fachada retrocompatível para o subsistema de captura e substituição segura."""
from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from universal_translator.domain.entities import WindowContext  # noqa: E402
from universal_translator.infrastructure.clipboard.safe_clipboard_engine import (  # noqa: E402
    SafeTextCaptureEngine,
)
from universal_translator.infrastructure.windows.win32_inspector import (  # noqa: E402
    Win32WindowInspector,
)

_default_inspector = Win32WindowInspector()
_default_engine = SafeTextCaptureEngine(window_inspector=_default_inspector)


def get_foreground_window() -> int | None:
    return _default_inspector.get_foreground_hwnd()


def capture_selection(hwnd: int | None = None) -> tuple[str, int | None]:
    res = _default_engine.capture_selection(target_hwnd=hwnd)
    captured_hwnd = res.window_context.hwnd if res.window_context else hwnd
    return res.text, captured_hwnd


def activate_window(hwnd: int) -> bool:
    ctx = _default_inspector.get_window_context_for_hwnd(int(hwnd))
    if ctx is None:
        ctx = WindowContext(hwnd=int(hwnd))
    return _default_inspector.activate_window_safely(ctx)


def replace_active_text(text: str, hwnd: int | None) -> tuple[bool, str]:
    if not hwnd:
        return False, "A janela original não está mais disponível."
    ctx = _default_inspector.get_window_context_for_hwnd(int(hwnd)) or WindowContext(hwnd=int(hwnd))
    res = _default_engine.replace_text_safely(
        new_text=text,
        original_context=ctx,
        verify_content_before_paste=False,
    )
    return res.success, res.message
