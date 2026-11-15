"""Serviços auxiliares de integração e gerenciamento de atalhos globais seguros."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..domain.enums import LogCategory
from ..infrastructure.logging.logger import ObservabilityLogger
from ..infrastructure.windows.win32_inspector import Win32WindowInspector


class GlobalHotkeyService:
    """Gerencia registro e limpeza segura de atalhos globais sem bloquear ou sequestrar atalhos nativos."""

    def __init__(
        self,
        window_inspector: Win32WindowInspector | None = None,
        logger: ObservabilityLogger | None = None,
    ) -> None:
        self.inspector = window_inspector or Win32WindowInspector()
        self.logger = logger or ObservabilityLogger.get_instance()
        self._handles: dict[str, Any] = {}
        self._keyboard_mod = self._load_keyboard()

    @staticmethod
    def _load_keyboard():
        try:
            import keyboard  # type: ignore
            return keyboard
        except Exception:
            return None

    def register_hotkey(
        self,
        hotkey: str,
        callback: Callable[[int], None],
        description: str = "",
    ) -> tuple[bool, str]:
        hk = (hotkey or "").strip().lower()
        if not hk:
            return False, "Atalho vazio."

        self.unregister_hotkey(hk)

        if self._keyboard_mod is None:
            msg = f"Módulo 'keyboard' indisponível neste ambiente ({hk})."
            self.logger.warning(LogCategory.WINDOWS_INTEGRATION, msg)
            return False, msg

        def _minimal_hook() -> None:
            hwnd = self.inspector.get_foreground_hwnd() or 0
            callback(int(hwnd))

        try:
            handle = self._keyboard_mod.add_hotkey(
                hk,
                _minimal_hook,
                suppress=False,
                trigger_on_release=True,
            )
            self._handles[hk] = handle
            self.logger.info(
                LogCategory.WINDOWS_INTEGRATION,
                f"Atalho global registrado: '{hk}' ({description}).",
            )
            return True, f"Atalho '{hk}' registrado com sucesso."
        except Exception as exc:
            err = f"Falha ao registrar atalho '{hk}': {exc}"
            self.logger.error(LogCategory.WINDOWS_INTEGRATION, err)
            return False, err

    def unregister_hotkey(self, hotkey: str) -> None:
        hk = (hotkey or "").strip().lower()
        handle = self._handles.pop(hk, None)
        if handle is not None and self._keyboard_mod is not None:
            try:
                self._keyboard_mod.remove_hotkey(handle)
            except Exception:
                pass

    def unregister_all(self) -> None:
        for hk in list(self._handles.keys()):
            self.unregister_hotkey(hk)
