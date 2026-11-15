"""Subsistema de integração nativa com o Windows."""
from .startup_manager import WindowsStartupManager
from .win32_inspector import Win32WindowInspector

__all__ = ["Win32WindowInspector", "WindowsStartupManager"]
