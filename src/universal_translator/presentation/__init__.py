"""Camada de Apresentação PySide6 do Universal Translator."""
from .styles import DARK_STYLESHEET, PALETTE
from .widgets import FloatingButton, FloatingPopupCard
from .windows import CaptureWorker, MainWindow, TranslationTaskWorker

__all__ = [
    "CaptureWorker",
    "DARK_STYLESHEET",
    "FloatingButton",
    "FloatingPopupCard",
    "MainWindow",
    "PALETTE",
    "TranslationTaskWorker",
]
