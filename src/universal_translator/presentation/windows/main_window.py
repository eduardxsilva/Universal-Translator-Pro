"""Janela Principal do Universal Translator Pro (PySide6) com 6 Seções e Monitoramento Inteligente Residente."""
from __future__ import annotations

import datetime
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:
    import keyboard
except ImportError:
    keyboard = None  # type: ignore[assignment]

try:
    import mouse
except ImportError:
    mouse = None  # type: ignore[assignment]

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QClipboard, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...application.auto_monitor import AutoCycleOutcome
from ...application.orchestrator import TranslationOrchestrator
from ...domain.entities import (
    ApplicationProfile,
    CaptureResult,
    GlossaryEntry,
    ReplacementResult,
    TranslationResult,
    WindowContext,
)
from ...domain.enums import (
    LANGUAGE_CODE_TO_NAME,
    SOURCE_LANGUAGES,
    SUPPORTED_LANGUAGES,
    CaptureMethod,
    DeepLAccountType,
    FloatingButtonState,
    FormalityLevel,
    LogCategory,
    ProviderType,
    TranslationDirection,
)
from ...domain.services import LanguageDetector
from ...services.hotkey_service import GlobalHotkeyService
from ..styles.theme import DARK_STYLESHEET
from ..widgets.floating_translator import FloatingButton, FloatingPopupCard


class CaptureWorker(QThread):
    """Executa captura de seleção ou leitura direta de cópia fora da GUI thread."""

    captured = Signal(object, object)  # (CaptureResult, ApplicationProfile)
    failed = Signal(str)

    def __init__(
        self,
        orchestrator: TranslationOrchestrator,
        target_hwnd: int | None = None,
        from_clipboard_directly: bool = False,
    ) -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self.target_hwnd = target_hwnd
        self.from_clipboard_directly = from_clipboard_directly

    def run(self) -> None:
        try:
            if self.from_clipboard_directly and hasattr(
                self.orchestrator.capture_engine, "capture_copied_clipboard"
            ):
                cap = self.orchestrator.capture_engine.capture_copied_clipboard(
                    target_hwnd=self.target_hwnd
                )
                profile = self.orchestrator.profile_matcher.match(cap.window_context)
                if cap.window_context is not None:
                    cap.window_context.matched_profile_name = profile.app_name
                if cap.success:
                    self.orchestrator._last_capture = cap
            else:
                cap, profile = self.orchestrator.capture_from_active_window(self.target_hwnd)
            self.captured.emit(cap, profile)
        except Exception as exc:
            self.failed.emit(str(exc))


class PreTranslateWorker(QThread):
    """Traduz automaticamente o texto capturado em segundo plano."""

    pretranslated = Signal(object)  # TranslationResult

    def __init__(
        self,
        orchestrator: TranslationOrchestrator,
        text: str,
        source_lang: str | None,
        target_lang: str,
        capture_result: CaptureResult | None,
        preferred_provider: ProviderType,
    ) -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self.text = text
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.capture_result = capture_result
        self.preferred_provider = preferred_provider

    def run(self) -> None:
        try:
            ctx = self.capture_result.window_context if self.capture_result else None
            res = self.orchestrator.translate(
                text=self.text,
                source_lang=self.source_lang,
                target_lang=self.target_lang,
                direction=TranslationDirection.OUTGOING_SENT,
                window_context=ctx,
                preferred_provider=self.preferred_provider,
            )
            self.pretranslated.emit(res)
        except Exception:
            pass


class TranslateAndReplaceWorker(QThread):
    """Executa captura sob demanda, tradução e substituição fora da thread de interface."""

    completed = Signal(object, object, object)  # (CaptureResult, TranslationResult, ReplacementResult | None)
    failed = Signal(str)

    def __init__(
        self,
        orchestrator: TranslationOrchestrator,
        target_hwnd: int | None,
        fallback_text: str,
        source_lang: str | None,
        target_lang: str,
        direction: TranslationDirection,
        existing_capture: CaptureResult | None,
        auto_replace: bool,
        preferred_provider: ProviderType | None = None,
        formality: FormalityLevel | None = None,
    ) -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self.target_hwnd = target_hwnd
        self.fallback_text = fallback_text
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.direction = direction
        self.existing_capture = existing_capture
        self.auto_replace = auto_replace
        self.preferred_provider = preferred_provider
        self.formality = formality
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            inspector = self.orchestrator.capture_engine.inspector
            hwnd = self.target_hwnd or inspector.get_foreground_hwnd()

            live_capture: CaptureResult | None = None
            if hwnd and inspector.is_window_valid(int(hwnd)):
                ctx_check = inspector.get_window_context_for_hwnd(int(hwnd))
                if ctx_check and not inspector.is_own_process(ctx_check):
                    cap_try, _ = self.orchestrator.capture_from_active_window(int(hwnd))
                    if cap_try.success and cap_try.text.strip():
                        live_capture = cap_try

            if live_capture is None:
                if (
                    self.existing_capture
                    and self.existing_capture.success
                    and self.existing_capture.text.strip()
                ):
                    live_capture = self.existing_capture
                elif hwnd and hasattr(self.orchestrator.capture_engine, "capture_copied_clipboard"):
                    clip_try = self.orchestrator.capture_engine.capture_copied_clipboard(int(hwnd))
                    if clip_try.success and clip_try.text.strip():
                        live_capture = clip_try

            if live_capture is None and self.fallback_text.strip():
                ctx = (
                    inspector.get_window_context_for_hwnd(int(hwnd))
                    if hwnd and inspector.is_window_valid(int(hwnd))
                    else WindowContext(hwnd=int(hwnd or 0))
                )
                live_capture = CaptureResult(
                    success=True,
                    text=self.fallback_text.strip(),
                    window_context=ctx,
                    method=CaptureMethod.MANUAL_BUFFER,
                )

            if live_capture is None or not live_capture.text.strip():
                self.failed.emit(
                    "Nenhum texto encontrado para traduzir. Digite em um campo compatível ou selecione o texto."
                )
                return

            if self._cancelled:
                return

            text_to_translate = live_capture.text.strip()
            resolved_src = (
                None
                if (not self.source_lang or self.source_lang.upper() == "AUTO")
                else self.source_lang
            )

            translation = self.orchestrator.translate(
                text=text_to_translate,
                source_lang=resolved_src,
                target_lang=self.target_lang,
                direction=self.direction,
                window_context=live_capture.window_context,
                preferred_provider=self.preferred_provider,
                formality=self.formality,
            )

            if self._cancelled:
                return

            replacement: ReplacementResult | None = None
            target_ctx = live_capture.window_context
            if target_ctx is None and hwnd:
                target_ctx = inspector.get_window_context_for_hwnd(int(hwnd)) or WindowContext(
                    hwnd=int(hwnd)
                )

            if self.auto_replace and target_ctx is not None and target_ctx.hwnd:
                replacement = self.orchestrator.capture_engine.replace_text_safely(
                    new_text=translation.translated_text,
                    original_context=target_ctx,
                    expected_original_text=text_to_translate,
                    verify_content_before_paste=False,
                )

            self.completed.emit(live_capture, translation, replacement)
        except Exception as exc:
            if not self._cancelled:
                self.failed.emit(str(exc))


TranslationTaskWorker = TranslateAndReplaceWorker


class AutoTranslationWorker(QThread):
    """Worker de tradução assíncrona compatível com o modo automático."""

    translated = Signal(str, str)
    failed = Signal(str)

    def __init__(
        self,
        orchestrator: TranslationOrchestrator,
        text: str,
        source: str | None,
        target: str,
    ) -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self.text = text
        self.source = source
        self.target = target

    def run(self) -> None:
        try:
            result = self.orchestrator.translate(
                self.text, source_lang=self.source, target_lang=self.target
            )
            self.translated.emit(result.translated_text, self.text)
        except Exception as exc:
            self.failed.emit(str(exc))


class AutoCaptureWorker(QThread):
    """Worker assíncrono de captura integral."""

    captured = Signal(str, int, int)
    failed = Signal(str, int, int)

    def __init__(self, capture_fn: Callable[[int], str], hwnd: int, serial: int) -> None:
        super().__init__()
        self.capture_fn = capture_fn
        self.hwnd = hwnd
        self.serial = serial

    def run(self) -> None:
        try:
            self.captured.emit(self.capture_fn(self.hwnd), self.hwnd, self.serial)
        except Exception as exc:
            self.failed.emit(str(exc), self.hwnd, self.serial)


class AutoIdleCycleWorker(QThread):
    """Executa o ciclo completo de captura integral -> elegibilidade -> DeepL/Cache -> substituição segura."""

    cycle_finished = Signal(object)  # AutoCycleOutcome
    cycle_failed = Signal(str)

    def __init__(
        self,
        orchestrator: TranslationOrchestrator,
        source_lang: str | None,
        target_lang: str,
    ) -> None:
        super().__init__()
        self.orchestrator = orchestrator
        self.source_lang = source_lang
        self.target_lang = target_lang

    def run(self) -> None:
        try:
            outcome = self.orchestrator.auto_monitor.execute_idle_cycle(
                source_lang=self.source_lang,
                target_lang=self.target_lang,
            )
            self.cycle_finished.emit(outcome)
        except Exception as exc:
            self.cycle_failed.emit(str(exc))


class DeepLUsageWorker(QThread):
    """Consulta assíncrona ao endpoint oficial /v2/usage da DeepL."""

    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, orchestrator: TranslationOrchestrator) -> None:
        super().__init__()
        self.orchestrator = orchestrator

    def run(self) -> None:
        try:
            self.succeeded.emit(self.orchestrator.test_deepl_connection())
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QWidget):
    """Painel Principal do Universal Translator Pro estruturado nas 6 seções da especificação:
    1. Visão Geral
    2. Idiomas
    3. DeepL
    4. Traduções
    5. Consumo
    6. Automação
    """

    ctrl_a_detected = Signal(int)
    ctrl_c_detected = Signal(int)
    hotkey_capture_triggered = Signal(int)
    hotkey_inline_triggered = Signal(int)
    hotkey_incoming_triggered = Signal(int)
    auto_typing_signal = Signal(str, bool, bool)  # (key_name, ctrl_pressed, alt_pressed)
    auto_mouse_signal = Signal(str)

    def __init__(self, orchestrator: TranslationOrchestrator | None = None) -> None:
        super().__init__()
        self.orchestrator = orchestrator or TranslationOrchestrator()
        self.hotkey_service = GlobalHotkeyService()
        self.languages = dict(SUPPORTED_LANGUAGES)
        self.source_languages = dict(SOURCE_LANGUAGES)

        self.selected_text: str = ""
        self.pretranslated_text: str = ""
        self.current_source_lang_code: str = (
            "AUTO"
            if self.orchestrator.settings.auto_detect_source
            else (self.orchestrator.settings.source_language_code or "PT-BR")
        )
        self.target_hwnd: int | None = None
        self.active_capture: CaptureResult | None = None
        self.active_profile: ApplicationProfile = (
            self.orchestrator.profile_matcher.global_default_profile()
        )
        self.capture_worker: CaptureWorker | None = None
        self.pretranslate_worker: PreTranslateWorker | None = None
        self.translation_worker: TranslateAndReplaceWorker | None = None
        self._deepl_usage_worker: DeepLUsageWorker | None = None
        self._auto_cycle_worker: AutoIdleCycleWorker | None = None
        self._auto_worker: AutoTranslationWorker | None = None
        self._auto_capture_worker: AutoCaptureWorker | None = None
        self.closing = False

        self._auto_buffer = ""
        self._auto_lock = threading.RLock()
        self._auto_last_hwnd: int | None = None
        self._auto_activity_serial = 0
        self._auto_translation_serial = 0
        self._auto_internal_action = False
        self._auto_last_clipboard_seq = 0

        # Temporizador inteligente de inatividade (padrão 5 segundos, configurável)
        self._auto_idle_timer = QTimer(self)
        self._auto_idle_timer.setSingleShot(True)
        self._auto_idle_timer.setInterval(
            int(self.orchestrator.settings.auto_idle_timeout_seconds * 1000)
        )
        self._auto_idle_timer.timeout.connect(self._process_auto_typing)

        # Conecta o AutoTranslationMonitor ao QTimer da GUI thread
        self.orchestrator.auto_monitor._on_timer_restart = self._restart_idle_qtimer
        self.orchestrator.auto_monitor._on_timer_stop = self._stop_idle_qtimer

        # Polling leve de foco, campo e área de transferência durante o modo automático
        self._auto_clipboard_timer = QTimer(self)
        self._auto_clipboard_timer.setInterval(200)
        self._auto_clipboard_timer.timeout.connect(self._poll_focus_and_clipboard_activity)

        self.auto_typing_signal.connect(self._on_auto_key_event)
        self.auto_mouse_signal.connect(self._on_auto_mouse_event)

        self.setWindowTitle("Universal Translator Pro — Camada Inteligente de Tradução Residente")
        self.setMinimumSize(980, 700)
        self.setStyleSheet(DARK_STYLESHEET)

        # Botão Flutuante Independente (3 áreas interativas)
        self.floating = FloatingButton(self)
        self.floating.set_saved_position(
            self.orchestrator.settings.floating_pos_x,
            self.orchestrator.settings.floating_pos_y,
        )
        self.floating.set_auto_detect(self.orchestrator.settings.auto_detect_source)
        self.floating.set_source_language(
            "AUTO"
            if self.orchestrator.settings.auto_detect_source
            else self.orchestrator.settings.source_language
        )
        self.floating.set_target_language(self.orchestrator.settings.language)
        self.floating.panel_opened_for_hwnd.connect(self._on_floating_panel_opened)
        self.floating.translate_outgoing_requested.connect(self.translate_selection)
        self.floating.translate_incoming_requested.connect(self.translate_incoming_selection)
        self.floating.rollback_requested.connect(self.rollback_last_action)
        self.floating.cancel_requested.connect(self.cancel_active_translation)
        self.floating.position_changed.connect(self._on_floating_moved)
        self.floating.closed_by_user.connect(self._on_floating_closed_by_user)
        self.floating.source_language_changed.connect(self._on_floating_source_lang_changed)
        self.floating.target_language_changed.connect(self._on_floating_target_lang_changed)
        self.floating.auto_detect_changed.connect(self._on_floating_auto_detect_changed)
        self.floating.open_language_settings_requested.connect(self._open_languages_tab_from_floating)
        self.floating.automatic_mode_changed.connect(self._on_automatic_mode_changed)

        self._keyboard_hook = None
        if keyboard is not None:
            try:
                self._keyboard_hook = keyboard.on_press(
                    self._global_key_for_auto_mode, suppress=False
                )
            except Exception:
                self._keyboard_hook = None

        self._mouse_hook = None
        if mouse is not None:
            try:
                self._mouse_hook = mouse.hook(self._global_mouse_for_auto_mode)
            except Exception:
                self._mouse_hook = None

        self.popup_card = FloatingPopupCard()
        self.popup_card.copy_requested.connect(self._copy_to_clipboard)
        self.popup_card.apply_replacement_requested.connect(self._replace_from_popup)

        self._build_ui()
        self._create_tray()

        self.ctrl_a_detected.connect(self.schedule_capture)
        self.ctrl_c_detected.connect(self.schedule_clipboard_capture)
        self.hotkey_capture_triggered.connect(self._on_hotkey_capture)
        self.hotkey_inline_triggered.connect(self._on_hotkey_inline_translate)
        self.hotkey_incoming_triggered.connect(self._on_hotkey_incoming_translate)

        self.register_all_hotkeys()
        if self.orchestrator.settings.show_floating_button:
            self.floating.show()
            self.float_toggle_btn.setText("Ocultar Botão Flutuante")
        else:
            self.floating.hide()
            self.float_toggle_btn.setText("Exibir Botão Flutuante")

        if self.orchestrator.settings.automatic_mode_enabled and self.orchestrator.settings.show_floating_button:
            self.floating.set_automatic_mode(True, emit_signal=True)

        self.refresh_all_views()

    def _restart_idle_qtimer(self, interval_ms: int) -> None:
        self._auto_idle_timer.start(max(1000, int(interval_ms)))

    def _stop_idle_qtimer(self) -> None:
        self._auto_idle_timer.stop()

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(16, 14, 16, 14)
        root_layout.setSpacing(12)

        header_layout = QHBoxLayout()
        title_box = QVBoxLayout()
        title_lbl = QLabel("Universal Translator Pro")
        title_lbl.setObjectName("HeaderTitle")
        subtitle_lbl = QLabel(
            "Tradutor Residente Integrado ao Windows • Monitoramento Automático de 5s • API Oficial DeepL"
        )
        subtitle_lbl.setObjectName("SubTitle")
        title_box.addWidget(title_lbl)
        title_box.addWidget(subtitle_lbl)
        header_layout.addLayout(title_box)
        header_layout.addStretch()

        self.auto_state_badge = QLabel("Modo Automático: Inativo")
        self.auto_state_badge.setObjectName("BadgeLabel")
        self.auto_state_badge.setStyleSheet(
            "background-color: #344054; color: #F2F4F7; padding: 5px 12px; border-radius: 11px; font-weight: 700;"
        )
        header_layout.addWidget(self.auto_state_badge)

        self.profile_badge = QLabel("Perfil: Padrão Global")
        self.profile_badge.setObjectName("BadgeLabel")
        header_layout.addWidget(self.profile_badge)

        self.float_toggle_btn = QPushButton("Ocultar Botão Flutuante")
        self.float_toggle_btn.setObjectName("SecondaryButton")
        self.float_toggle_btn.clicked.connect(self.toggle_float)
        header_layout.addWidget(self.float_toggle_btn)

        root_layout.addLayout(header_layout)

        # 6 Seções do Painel Principal conforme Seção 6.1 da especificação
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview_tab(), "1. Visão Geral")
        self.tabs.addTab(self._build_languages_tab(), "2. Idiomas")
        self.tabs.addTab(self._build_deepl_tab(), "3. DeepL")
        self.tabs.addTab(self._build_translations_tab(), "4. Traduções")
        self.tabs.addTab(self._build_consumption_tab(), "5. Consumo")
        self.tabs.addTab(self._build_automation_tab(), "6. Automação")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root_layout.addWidget(self.tabs)

        footer = QHBoxLayout()
        self.status = QLabel(
            "Status operacional: Pronto. Ative o botão flutuante central para tradução automática após 5s de inatividade."
        )
        self.status.setWordWrap(True)
        footer.addWidget(self.status, stretch=1)
        root_layout.addLayout(footer)

    # -------------------------------------------------------------------------
    # SEÇÃO 1: VISÃO GERAL
    # -------------------------------------------------------------------------
    def _build_overview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        # Cards de resumo operacional (Estado, Idiomas, Conexão DeepL, Última Tradução)
        cards_grid = QGridLayout()
        cards_grid.setSpacing(10)

        state_card = QFrame()
        state_card.setObjectName("CardFrame")
        sc_layout = QVBoxLayout(state_card)
        sc_layout.setContentsMargins(12, 10, 12, 10)
        sc_title = QLabel("ESTADO DO TRADUTOR")
        sc_title.setStyleSheet("font-size: 11px; color: #98A2B3; font-weight: 700;")
        self.overview_state_lbl = QLabel("Inativo (Aguardando ativação)")
        self.overview_state_lbl.setStyleSheet("font-size: 15px; font-weight: 700; color: #FFFFFF;")
        self.overview_toggle_auto_btn = QPushButton("Ativar Tradução Automática (5s)")
        self.overview_toggle_auto_btn.setObjectName("SuccessButton")
        self.overview_toggle_auto_btn.clicked.connect(self.floating.toggle_automatic_mode)
        sc_layout.addWidget(sc_title)
        sc_layout.addWidget(self.overview_state_lbl)
        sc_layout.addWidget(self.overview_toggle_auto_btn)
        cards_grid.addWidget(state_card, 0, 0)

        lang_card = QFrame()
        lang_card.setObjectName("CardFrame")
        lc_layout = QVBoxLayout(lang_card)
        lc_layout.setContentsMargins(12, 10, 12, 10)
        lc_title = QLabel("IDIOMAS CONFIGURADOS")
        lc_title.setStyleSheet("font-size: 11px; color: #98A2B3; font-weight: 700;")
        self.overview_langs_lbl = QLabel("Origem: Detecção Automática → Destino: English (EUA)")
        self.overview_langs_lbl.setStyleSheet("font-size: 14px; font-weight: 600; color: #FFFFFF;")
        self.overview_langs_lbl.setWordWrap(True)
        lc_btn = QPushButton("Configurar Idiomas")
        lc_btn.setObjectName("SecondaryButton")
        lc_btn.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        lc_layout.addWidget(lc_title)
        lc_layout.addWidget(self.overview_langs_lbl)
        lc_layout.addWidget(lc_btn)
        cards_grid.addWidget(lang_card, 0, 1)

        deepl_card = QFrame()
        deepl_card.setObjectName("CardFrame")
        dc_layout = QVBoxLayout(deepl_card)
        dc_layout.setContentsMargins(12, 10, 12, 10)
        dc_title = QLabel("CONEXÃO COM A DEEPL")
        dc_title.setStyleSheet("font-size: 11px; color: #98A2B3; font-weight: 700;")
        self.overview_deepl_lbl = QLabel("Verificando credencial no Windows Credential Manager...")
        self.overview_deepl_lbl.setStyleSheet("font-size: 13px; font-weight: 600; color: #D0D5DD;")
        self.overview_deepl_lbl.setWordWrap(True)
        dc_btn = QPushButton("Gerenciar Chave DeepL / Uso")
        dc_btn.setObjectName("SecondaryButton")
        dc_btn.clicked.connect(lambda: self.tabs.setCurrentIndex(2))
        dc_layout.addWidget(dc_title)
        dc_layout.addWidget(self.overview_deepl_lbl)
        dc_layout.addWidget(dc_btn)
        cards_grid.addWidget(deepl_card, 0, 2)

        layout.addLayout(cards_grid)

        # Painel de Última Tradução e Teste Rápido
        editors_layout = QHBoxLayout()

        left_box = QVBoxLayout()
        self.source_header_lbl = QLabel("Último Texto Capturado / Original:")
        left_box.addWidget(self.source_header_lbl)
        self.source_editor = QPlainTextEdit()
        self.source_editor.setPlaceholderText(
            "Digite normalmente em qualquer aplicativo com o Modo Automático ativo ou cole um texto aqui..."
        )
        self.source_editor.textChanged.connect(self._on_source_editor_changed)
        left_box.addWidget(self.source_editor)
        editors_layout.addLayout(left_box)

        right_box = QVBoxLayout()
        self.target_header_lbl = QLabel("Última Tradução Aplicada:")
        right_box.addWidget(self.target_header_lbl)
        self.target_editor = QPlainTextEdit()
        self.target_editor.setPlaceholderText("O resultado da tradução aparecerá aqui...")
        right_box.addWidget(self.target_editor)
        editors_layout.addLayout(right_box)

        layout.addLayout(editors_layout, stretch=1)

        self.selection_preview = QLabel("Última tradução: nenhuma operação realizada nesta sessão.")
        self.selection_preview.setStyleSheet(
            "background-color: #1D2939; padding: 8px 12px; border-radius: 6px; color: #D0D5DD;"
        )
        self.selection_preview.setWordWrap(True)
        layout.addWidget(self.selection_preview)

        actions_row = QHBoxLayout()
        self.translate_button = QPushButton("Traduzir e Substituir Agora")
        self.translate_button.setObjectName("SuccessButton")
        self.translate_button.clicked.connect(self.translate_selection)
        actions_row.addWidget(self.translate_button)

        self.copy_translation_btn = QPushButton("Copiar Tradução")
        self.copy_translation_btn.setObjectName("SecondaryButton")
        self.copy_translation_btn.clicked.connect(
            lambda: self._copy_to_clipboard(self.target_editor.toPlainText())
        )
        actions_row.addWidget(self.copy_translation_btn)

        self.rollback_btn = QPushButton("Desfazer Última Substituição (Rollback)")
        self.rollback_btn.setObjectName("DangerButton")
        self.rollback_btn.clicked.connect(self.rollback_last_action)
        actions_row.addWidget(self.rollback_btn)

        layout.addLayout(actions_row)
        return page

    # -------------------------------------------------------------------------
    # SEÇÃO 2: IDIOMAS
    # -------------------------------------------------------------------------
    def _build_languages_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        lang_card = QFrame()
        lang_card.setObjectName("CardFrame")
        grid = QGridLayout(lang_card)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)

        self.auto_detect_source_chk = QCheckBox(
            "Detecção automática do idioma de origem (recomendado para uso com DeepL)"
        )
        self.auto_detect_source_chk.setChecked(self.orchestrator.settings.auto_detect_source)
        self.auto_detect_source_chk.toggled.connect(self._on_ui_auto_detect_toggled)
        grid.addWidget(self.auto_detect_source_chk, 0, 0, 1, 4)

        grid.addWidget(QLabel("Idioma de Origem:"), 1, 0)
        self.source_language_combo = QComboBox()
        self.source_language_combo.addItems(list(self.languages.keys()))
        self.source_language_combo.setCurrentText(self.orchestrator.settings.source_language)
        self.source_language_combo.currentTextChanged.connect(self._on_quick_source_language_changed)
        grid.addWidget(self.source_language_combo, 1, 1)

        grid.addWidget(QLabel("Idioma de Destino:"), 1, 2)
        self.language = QComboBox()
        self.language.addItems(list(self.languages.keys()))
        self.language.setCurrentText(self.orchestrator.settings.language)
        self.language.currentTextChanged.connect(self._on_quick_language_changed)
        grid.addWidget(self.language, 1, 3)

        grid.addWidget(QLabel("Formalidade (DeepL):"), 2, 0)
        self.quick_formality_combo = QComboBox()
        for f in FormalityLevel:
            self.quick_formality_combo.addItem(f.name.title(), f.value)
        f_idx = self.quick_formality_combo.findData(self.orchestrator.settings.formality)
        if f_idx >= 0:
            self.quick_formality_combo.setCurrentIndex(f_idx)
        grid.addWidget(self.quick_formality_combo, 2, 1)

        grid.addWidget(QLabel("Idioma de Leitura (Recebidas):"), 2, 2)
        self.incoming_language_combo = QComboBox()
        self.incoming_language_combo.addItems(list(self.languages.keys()))
        self.incoming_language_combo.setCurrentText(self.orchestrator.settings.incoming_language)
        grid.addWidget(self.incoming_language_combo, 2, 3)

        save_lang_btn = QPushButton("Salvar Configurações de Idiomas")
        save_lang_btn.setObjectName("SuccessButton")
        save_lang_btn.clicked.connect(self.save)
        grid.addWidget(save_lang_btn, 3, 0, 1, 4)

        layout.addWidget(lang_card)

        # Configurações Avançadas de Idiomas: Glossário / Memória Linguística
        gloss_lbl = QLabel(
            "Configurações Avançadas de Idioma — Glossário Técnico & Termos Protegidos:"
        )
        gloss_lbl.setStyleSheet("font-weight: 700; color: #FFFFFF;")
        layout.addWidget(gloss_lbl)

        self.glossary_table = QTableWidget(0, 6)
        self.glossary_table.setHorizontalHeaderLabels(
            ["ID", "Termo Original", "Tradução Preferida", "Idiomas", "Não Traduzir", "Domínio"]
        )
        self.glossary_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.glossary_table, stretch=1)

        form_card = QFrame()
        form_card.setObjectName("CardFrame")
        g_grid = QGridLayout(form_card)
        g_grid.setContentsMargins(12, 10, 12, 10)

        self.gloss_src_input = QLineEdit()
        self.gloss_tgt_input = QLineEdit()
        self.gloss_domain_input = QLineEdit("geral")
        self.gloss_dnt_chk = QCheckBox("Não traduzir (preservar termo exato)")
        self.gloss_case_chk = QCheckBox("Diferenciar maiúsculas/minúsculas")

        g_grid.addWidget(QLabel("Termo Original:"), 0, 0)
        g_grid.addWidget(self.gloss_src_input, 0, 1)
        g_grid.addWidget(QLabel("Tradução Preferida:"), 0, 2)
        g_grid.addWidget(self.gloss_tgt_input, 0, 3)
        g_grid.addWidget(QLabel("Domínio:"), 1, 0)
        g_grid.addWidget(self.gloss_domain_input, 1, 1)
        g_grid.addWidget(self.gloss_dnt_chk, 1, 2)
        g_grid.addWidget(self.gloss_case_chk, 1, 3)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Adicionar Termo ao Glossário")
        add_btn.setObjectName("SuccessButton")
        add_btn.clicked.connect(self._add_glossary_from_ui)
        btn_row.addWidget(add_btn)

        del_btn = QPushButton("Remover Termo Selecionado")
        del_btn.setObjectName("DangerButton")
        del_btn.clicked.connect(self._delete_selected_glossary)
        btn_row.addWidget(del_btn)
        g_grid.addLayout(btn_row, 2, 0, 1, 4)

        layout.addWidget(form_card)
        return page

    # -------------------------------------------------------------------------
    # SEÇÃO 3: DEEPL
    # -------------------------------------------------------------------------
    def _build_deepl_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        deepl_card = QFrame()
        deepl_card.setObjectName("CardFrame")
        form = QGridLayout(deepl_card)
        form.setContentsMargins(14, 14, 14, 14)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        vault_lbl = QLabel(
            f"Segurança de Credenciais: {self.orchestrator.settings_manager.credentials.backend_name} "
            "(A chave nunca é gravada em settings.json ou nos logs)"
        )
        vault_lbl.setStyleSheet("color: #16A36A; font-weight: 700;")
        form.addWidget(vault_lbl, 0, 0, 1, 4)

        form.addWidget(QLabel("DeepL API Key:"), 1, 0)
        self.api = QLineEdit(
            self.orchestrator.settings_manager.get_provider_api_key(ProviderType.DEEPL)
        )
        self.api.setEchoMode(QLineEdit.EchoMode.Password)
        self.api.setPlaceholderText("Insira sua chave DeepL API Free (:fx) ou DeepL Pro")
        form.addWidget(self.api, 1, 1, 1, 3)

        form.addWidget(QLabel("Tipo de Conta DeepL:"), 2, 0)
        self.deepl_account_type_combo = QComboBox()
        self.deepl_account_type_combo.addItem(
            "Automático (Detecta :fx para Free ou Pro)", DeepLAccountType.AUTO.value
        )
        self.deepl_account_type_combo.addItem(
            "DeepL API Free (api-free.deepl.com)", DeepLAccountType.FREE.value
        )
        self.deepl_account_type_combo.addItem(
            "DeepL API Pro (api.deepl.com)", DeepLAccountType.PRO.value
        )
        acc_idx = self.deepl_account_type_combo.findData(
            self.orchestrator.settings.deepl_account_type
        )
        if acc_idx >= 0:
            self.deepl_account_type_combo.setCurrentIndex(acc_idx)
        form.addWidget(self.deepl_account_type_combo, 2, 1)

        form.addWidget(QLabel("Mecanismo Oficial:"), 2, 2)
        self.quick_provider_combo = QComboBox()
        for p in ProviderType:
            self.quick_provider_combo.addItem(p.value.upper(), p.value)
        p_idx = self.quick_provider_combo.findData(self.orchestrator.settings.active_provider)
        if p_idx >= 0:
            self.quick_provider_combo.setCurrentIndex(p_idx)
        form.addWidget(self.quick_provider_combo, 2, 3)

        btn_row = QHBoxLayout()
        self.deepl_test_button = QPushButton("Testar Conexão (/v2/usage)")
        self.deepl_test_button.clicked.connect(self.test_deepl_connection)
        btn_row.addWidget(self.deepl_test_button)

        self.deepl_usage_btn = QPushButton("Consultar Utilização Agora")
        self.deepl_usage_btn.setObjectName("SecondaryButton")
        self.deepl_usage_btn.clicked.connect(self.test_deepl_connection)
        btn_row.addWidget(self.deepl_usage_btn)

        self.save_button = QPushButton("Salvar Credencial no Windows Credential Manager")
        self.save_button.setObjectName("SuccessButton")
        self.save_button.clicked.connect(self.save)
        btn_row.addWidget(self.save_button)
        form.addLayout(btn_row, 3, 0, 1, 4)

        self.deepl_auth_status_label = QLabel("Status de autenticação: aguardando verificação.")
        self.deepl_auth_status_label.setStyleSheet("font-weight: 700; color: #F2F4F7;")
        form.addWidget(self.deepl_auth_status_label, 4, 0, 1, 4)

        self.deepl_usage_label = QLabel("Uso DeepL: ainda não consultado.")
        self.deepl_usage_label.setWordWrap(True)
        self.deepl_usage_label.setStyleSheet(
            "background-color: #101828; padding: 10px; border-radius: 6px; color: #D0D5DD;"
        )
        form.addWidget(self.deepl_usage_label, 5, 0, 1, 4)

        # Campos secundários preservados para retrocompatibilidade
        self.google_api_input = QLineEdit(
            self.orchestrator.settings_manager.get_provider_api_key(ProviderType.GOOGLE)
        )
        self.google_api_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.ms_api_input = QLineEdit(
            self.orchestrator.settings_manager.get_provider_api_key(ProviderType.MICROSOFT)
        )
        self.ms_api_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_api_input = QLineEdit(
            self.orchestrator.settings_manager.get_provider_api_key(ProviderType.OPENAI)
        )
        self.openai_api_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.google_api_input.hide()
        self.ms_api_input.hide()
        self.openai_api_input.hide()

        layout.addWidget(deepl_card)
        layout.addStretch()
        return page

    # -------------------------------------------------------------------------
    # SEÇÃO 4: TRADUÇÕES
    # -------------------------------------------------------------------------
    def _build_translations_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        top_bar = QHBoxLayout()
        self.history_search_input = QLineEdit()
        self.history_search_input.setPlaceholderText("Filtrar traduções por texto original, tradução ou aplicativo...")
        self.history_search_input.textChanged.connect(self.refresh_history_table)
        top_bar.addWidget(self.history_search_input, stretch=1)

        copy_hist_btn = QPushButton("Copiar Tradução Selecionada")
        copy_hist_btn.setObjectName("SecondaryButton")
        copy_hist_btn.clicked.connect(self._copy_selected_history)
        top_bar.addWidget(copy_hist_btn)

        clear_hist_btn = QPushButton("Limpar Histórico")
        clear_hist_btn.setObjectName("DangerButton")
        clear_hist_btn.clicked.connect(self._purge_history)
        top_bar.addWidget(clear_hist_btn)

        layout.addLayout(top_bar)

        self.history_table = QTableWidget(0, 7)
        self.history_table.setHorizontalHeaderLabels(
            [
                "Data e Hora",
                "Aplicativo",
                "Direção",
                "Idiomas",
                "Texto Original",
                "Texto Traduzido",
                "Origem (Cache / API)",
            ]
        )
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.itemSelectionChanged.connect(self._on_history_row_selected)
        layout.addWidget(self.history_table, stretch=1)

        detail_box = QHBoxLayout()
        self.history_orig_detail = QPlainTextEdit()
        self.history_orig_detail.setReadOnly(True)
        self.history_orig_detail.setPlaceholderText("Selecione um registro acima para inspecionar o texto original...")
        self.history_orig_detail.setMaximumHeight(110)
        detail_box.addWidget(self.history_orig_detail)

        self.history_trans_detail = QPlainTextEdit()
        self.history_trans_detail.setReadOnly(True)
        self.history_trans_detail.setPlaceholderText("Selecione um registro acima para inspecionar o texto traduzido...")
        self.history_trans_detail.setMaximumHeight(110)
        detail_box.addWidget(self.history_trans_detail)
        layout.addLayout(detail_box)

        return page

    # -------------------------------------------------------------------------
    # SEÇÃO 5: CONSUMO
    # -------------------------------------------------------------------------
    def _build_consumption_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        summary_card = QFrame()
        summary_card.setObjectName("CardFrame")
        s_grid = QGridLayout(summary_card)
        s_grid.setContentsMargins(14, 12, 14, 12)
        s_grid.setHorizontalSpacing(16)
        s_grid.setVerticalSpacing(8)

        self.consumption_api_used_lbl = QLabel("Caracteres utilizados (API DeepL): N/A")
        self.consumption_api_used_lbl.setStyleSheet("font-weight: 700; color: #FFFFFF;")
        self.consumption_api_limit_lbl = QLabel("Limite informado pela API: N/A")
        self.consumption_api_remaining_lbl = QLabel("Saldo disponível (API DeepL): N/A")
        self.consumption_api_remaining_lbl.setStyleSheet("font-weight: 700; color: #16A36A;")

        self.consumption_local_confirmed_lbl = QLabel("Confirmados em respostas locais (/v2/translate): 0")
        self.consumption_local_estimated_lbl = QLabel("Estimativa local não confirmada: 0")
        self.consumption_cache_saved_lbl = QLabel("Economizados via Cache SQLite: 0")
        self.consumption_cache_saved_lbl.setStyleSheet("color: #60A5FA; font-weight: 600;")

        s_grid.addWidget(self.consumption_api_used_lbl, 0, 0)
        s_grid.addWidget(self.consumption_api_limit_lbl, 0, 1)
        s_grid.addWidget(self.consumption_api_remaining_lbl, 0, 2)
        s_grid.addWidget(self.consumption_local_confirmed_lbl, 1, 0)
        s_grid.addWidget(self.consumption_local_estimated_lbl, 1, 1)
        s_grid.addWidget(self.consumption_cache_saved_lbl, 1, 2)

        s_grid.addWidget(QLabel("Alerta configurável de utilização (%):"), 2, 0)
        self.deepl_alert_spin = QSpinBox()
        self.deepl_alert_spin.setRange(10, 100)
        self.deepl_alert_spin.setSingleStep(5)
        self.deepl_alert_spin.setValue(int(self.orchestrator.settings.deepl_alert_threshold_percent))
        s_grid.addWidget(self.deepl_alert_spin, 2, 1)

        refresh_usage_btn = QPushButton("Atualizar Saldo na DeepL (/v2/usage)")
        refresh_usage_btn.clicked.connect(self.test_deepl_connection)
        s_grid.addWidget(refresh_usage_btn, 2, 2)

        self.consumption_alert_banner = QLabel(
            "Nota: Os saldos oficiais provêm exclusivamente do endpoint /v2/usage da DeepL sem presumir renovação mensal."
        )
        self.consumption_alert_banner.setWordWrap(True)
        s_grid.addWidget(self.consumption_alert_banner, 3, 0, 1, 3)

        layout.addWidget(summary_card)

        layout.addWidget(QLabel("Histórico de Consumo Local (Confirmado vs Estimado vs Cache):"))
        self.usage_events_table = QTableWidget(0, 5)
        self.usage_events_table.setHorizontalHeaderLabels(
            ["Data/Hora", "Caracteres", "Classificação", "Origem", "Par de Idiomas"]
        )
        self.usage_events_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.usage_events_table, stretch=1)

        layout.addWidget(QLabel("Métricas Operacionais e Circuit Breaker:"))
        self.metrics_table = QTableWidget(0, 7)
        self.metrics_table.setHorizontalHeaderLabels(
            [
                "Provedor",
                "Circuit Breaker",
                "Requisições",
                "Cache Hits",
                "Falhas",
                "Caracteres Tarifados",
                "Custo Estimado (USD)",
            ]
        )
        self.metrics_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.metrics_table, stretch=1)
        return page

    # -------------------------------------------------------------------------
    # SEÇÃO 6: AUTOMAÇÃO
    # -------------------------------------------------------------------------
    def _build_automation_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        auto_card = QFrame()
        auto_card.setObjectName("CardFrame")
        a_grid = QGridLayout(auto_card)
        a_grid.setContentsMargins(12, 10, 12, 10)
        a_grid.setHorizontalSpacing(14)
        a_grid.setVerticalSpacing(8)

        self.auto_mode_chk = QCheckBox("Ativar Modo de Tradução Automática (monitoramento residente)")
        self.auto_mode_chk.setChecked(self.floating.automatic_mode)
        self.auto_mode_chk.toggled.connect(self._on_auto_mode_chk_toggled)
        a_grid.addWidget(self.auto_mode_chk, 0, 0, 1, 2)

        self.start_with_windows_chk = QCheckBox("Iniciar automaticamente com o Windows")
        self.start_with_windows_chk.setChecked(self.orchestrator.settings.start_with_windows)
        a_grid.addWidget(self.start_with_windows_chk, 0, 2, 1, 2)

        a_grid.addWidget(QLabel("Intervalo de inatividade (segundos):"), 1, 0)
        self.idle_timeout_spin = QSpinBox()
        self.idle_timeout_spin.setRange(2, 30)
        self.idle_timeout_spin.setValue(int(self.orchestrator.settings.auto_idle_timeout_seconds))
        a_grid.addWidget(self.idle_timeout_spin, 1, 1)

        a_grid.addWidget(QLabel("Timeout HTTP da API (segundos):"), 1, 2)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(3, 60)
        self.timeout_spin.setValue(int(self.orchestrator.settings.request_timeout_seconds))
        a_grid.addWidget(self.timeout_spin, 1, 3)

        self.prefer_uia_chk = QCheckBox(
            "Priorizar Windows UI Automation / Win32 antes de usar área de transferência"
        )
        self.prefer_uia_chk.setChecked(self.orchestrator.settings.prefer_uia_capture)
        a_grid.addWidget(self.prefer_uia_chk, 2, 0, 1, 2)

        self.verify_replace_chk = QCheckBox(
            "Validar foco e integridade do conteúdo antes de substituir"
        )
        self.verify_replace_chk.setChecked(self.orchestrator.settings.verify_before_replace)
        a_grid.addWidget(self.verify_replace_chk, 2, 2, 1, 2)

        self.protect_sensitive_chk = QCheckBox(
            "Bloquear campos de senha, cofres e janelas sensíveis"
        )
        self.protect_sensitive_chk.setChecked(self.orchestrator.settings.protect_sensitive_fields)
        a_grid.addWidget(self.protect_sensitive_chk, 3, 0, 1, 2)

        self.privacy_mode_chk = QCheckBox(
            "Modo de Privacidade Estrita (Não gravar histórico local de traduções)"
        )
        self.privacy_mode_chk.setChecked(self.orchestrator.settings.privacy_no_history)
        self.privacy_mode_chk.toggled.connect(self._on_privacy_mode_toggled)
        a_grid.addWidget(self.privacy_mode_chk, 3, 2, 1, 2)

        self.legacy_ctrl_a_chk = QCheckBox(
            "Capturar seleção ao pressionar Ctrl+A / Ctrl+C (atalho complementar)"
        )
        self.legacy_ctrl_a_chk.setChecked(self.orchestrator.settings.enable_legacy_ctrl_a_hook)
        a_grid.addWidget(self.legacy_ctrl_a_chk, 4, 0, 1, 2)

        a_grid.addWidget(QLabel("Atalho Tradução Inline:"), 4, 2)
        self.inline_hotkey_input = QLineEdit(self.orchestrator.settings.inline_translate_hotkey)
        a_grid.addWidget(self.inline_hotkey_input, 4, 3)

        self.capture_hotkey_input = QLineEdit(self.orchestrator.settings.capture_hotkey)
        self.incoming_hotkey_input = QLineEdit(self.orchestrator.settings.incoming_translate_hotkey)
        self.capture_hotkey_input.hide()
        self.incoming_hotkey_input.hide()

        save_auto_btn = QPushButton("Salvar Configurações de Automação e Privacidade")
        save_auto_btn.setObjectName("SuccessButton")
        save_auto_btn.clicked.connect(self.save)
        a_grid.addWidget(save_auto_btn, 5, 0, 1, 4)

        layout.addWidget(auto_card)

        # Aplicativos Compatíveis / Perfis
        layout.addWidget(QLabel("Aplicativos Compatíveis e Perfis de Contexto:"))
        self.profiles_table = QTableWidget(0, 7)
        self.profiles_table.setHorizontalHeaderLabels(
            [
                "Aplicativo",
                "Processos (.exe)",
                "Padrões de Título",
                "Envio (Destino)",
                "Recebidas",
                "Formalidade",
                "Revisar Antes",
            ]
        )
        self.profiles_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.profiles_table, stretch=1)

        prof_form = QFrame()
        prof_form.setObjectName("CardFrame")
        pf_grid = QGridLayout(prof_form)
        pf_grid.setContentsMargins(10, 8, 10, 8)
        self.prof_name_input = QLineEdit()
        self.prof_proc_input = QLineEdit()
        self.prof_title_input = QLineEdit()
        self.prof_target_combo = QComboBox()
        for name, code in self.languages.items():
            self.prof_target_combo.addItem(f"{name} ({code})", code)
        self.prof_formality_combo = QComboBox()
        for f in FormalityLevel:
            self.prof_formality_combo.addItem(f.name.title(), f.value)
        self.prof_review_chk = QCheckBox("Revisar antes")

        pf_grid.addWidget(QLabel("Perfil:"), 0, 0)
        pf_grid.addWidget(self.prof_name_input, 0, 1)
        pf_grid.addWidget(QLabel("Processos:"), 0, 2)
        pf_grid.addWidget(self.prof_proc_input, 0, 3)
        pf_grid.addWidget(QLabel("Títulos:"), 1, 0)
        pf_grid.addWidget(self.prof_title_input, 1, 1)
        pf_grid.addWidget(QLabel("Destino:"), 1, 2)
        pf_grid.addWidget(self.prof_target_combo, 1, 3)

        save_prof_btn = QPushButton("Adicionar / Atualizar Aplicativo Compatível")
        save_prof_btn.clicked.connect(self._save_profile_from_ui)
        pf_grid.addWidget(save_prof_btn, 2, 0, 1, 2)

        run_diag_btn = QPushButton("Executar Autodiagnóstico")
        run_diag_btn.setObjectName("SecondaryButton")
        run_diag_btn.clicked.connect(self.run_self_diagnostics)
        pf_grid.addWidget(run_diag_btn, 2, 2)

        export_logs_btn = QPushButton("Exportar Logs Sanitizados")
        export_logs_btn.setObjectName("SecondaryButton")
        export_logs_btn.clicked.connect(self.export_logs_dialog)
        pf_grid.addWidget(export_logs_btn, 2, 3)

        layout.addWidget(prof_form)

        # Tabela de Diagnóstico mantida para auditoria e compatibilidade
        self.diag_category_combo = QComboBox()
        self.diag_category_combo.addItem("TODOS")
        for cat in LogCategory:
            self.diag_category_combo.addItem(cat.value)
        self.diag_category_combo.hide()

        self.diagnostics_table = QTableWidget(0, 5)
        self.diagnostics_table.setHorizontalHeaderLabels(
            ["Horário", "Nível", "Categoria", "ID Operação", "Mensagem (Sanitizada)"]
        )
        self.diagnostics_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.diagnostics_table.setMaximumHeight(110)
        layout.addWidget(self.diagnostics_table)
        return page

    # -------------------------------------------------------------------------
    # BANDEJA DO SISTEMA (SYSTEM TRAY)
    # -------------------------------------------------------------------------
    def _create_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(
            QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        )
        self.tray.setToolTip("Universal Translator Pro")
        menu = QMenu()
        show_action = QAction("Abrir Painel Principal", self)
        show_action.triggered.connect(self.show_and_activate)
        auto_action = QAction("Ativar/Desativar Tradução Automática", self)
        auto_action.triggered.connect(self.floating.toggle_automatic_mode)
        toggle_action = QAction("Mostrar/Ocultar Botão Flutuante", self)
        toggle_action.triggered.connect(self.toggle_float)
        quit_action = QAction("Encerrar Universal Translator Pro", self)
        quit_action.triggered.connect(self.quit_app)
        for act in (show_action, auto_action, toggle_action, quit_action):
            menu.addAction(act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason: Any) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_and_activate()

    def show_and_activate(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _open_languages_tab_from_floating(self) -> None:
        self.show_and_activate()
        self.tabs.setCurrentIndex(1)

    def register_all_hotkeys(self) -> None:
        self.hotkey_service.unregister_all()
        s = self.orchestrator.settings

        if s.enable_legacy_ctrl_a_hook:
            self.hotkey_service.register_hotkey(
                "ctrl+a",
                lambda hwnd: self.ctrl_a_detected.emit(hwnd),
                "Captura complementar ao pressionar Ctrl+A",
            )
        if s.enable_ctrl_c_hook:
            self.hotkey_service.register_hotkey(
                "ctrl+c",
                lambda hwnd: self.ctrl_c_detected.emit(hwnd),
                "Captura complementar ao pressionar Ctrl+C",
            )

        self.hotkey_service.register_hotkey(
            s.capture_hotkey,
            lambda hwnd: self.hotkey_capture_triggered.emit(hwnd),
            "Capturar seleção atual",
        )
        self.hotkey_service.register_hotkey(
            s.inline_translate_hotkey,
            lambda hwnd: self.hotkey_inline_triggered.emit(hwnd),
            "Tradução inline imediata",
        )
        self.hotkey_service.register_hotkey(
            s.incoming_translate_hotkey,
            lambda hwnd: self.hotkey_incoming_triggered.emit(hwnd),
            "Traduzir mensagem recebida",
        )

    # -------------------------------------------------------------------------
    # MONITORAMENTO AUTOMÁTICO E TEMPORIZADOR INTELIGENTE DE 5 SEGUNDOS
    # -------------------------------------------------------------------------
    def _global_key_for_auto_mode(self, event: Any) -> None:
        """Hook global de teclado para o modo automático."""
        if (
            self._auto_internal_action
            or self.orchestrator.auto_monitor.internal_action
            or not self.floating.automatic_mode
            or getattr(event, "event_type", "") != "down"
        ):
            return
        name = str(getattr(event, "name", "") or "").lower()
        if not name:
            return
        ctrl_down = False
        alt_down = False
        if keyboard is not None:
            try:
                ctrl_down = bool(keyboard.is_pressed("ctrl"))
                alt_down = bool(keyboard.is_pressed("alt"))
            except Exception:
                pass
        self._auto_activity_serial += 1
        self.auto_typing_signal.emit(name, ctrl_down, alt_down)

    def _global_mouse_for_auto_mode(self, event: Any) -> None:
        if (
            not self.floating.automatic_mode
            or self._auto_internal_action
            or self.orchestrator.auto_monitor.internal_action
        ):
            return
        cls_name = event.__class__.__name__
        if cls_name in ("ButtonEvent", "WheelEvent"):
            self._auto_activity_serial += 1
            self.auto_mouse_signal.emit(cls_name)

    def _poll_focus_and_clipboard_activity(self) -> None:
        """Verifica periodicamente mudanças de janela/campo e colagens via clipboard."""
        if (
            not self.floating.automatic_mode
            or self._auto_internal_action
            or self.orchestrator.auto_monitor.internal_action
        ):
            return
        try:
            inspector = self.orchestrator.capture_engine.inspector
            fg = inspector.get_foreground_hwnd()
            if fg:
                ctx = inspector.get_window_context_for_hwnd(int(fg))
                if ctx and not inspector.is_own_process(ctx):
                    _, ctrl_id = self.orchestrator.capture_engine.uia.get_focused_control_identifier(
                        int(fg)
                    )
                    if self.orchestrator.auto_monitor.on_window_or_field_changed(int(fg), ctrl_id):
                        self.floating.set_visual_state(
                            FloatingButtonState.IDLE,
                            "Monitoramento ativo — mudança de janela/campo detectada",
                        )

            current_seq = inspector.get_clipboard_sequence_number()
            if current_seq and current_seq != self._auto_last_clipboard_seq:
                self._auto_last_clipboard_seq = current_seq
                if fg:
                    _, ctrl_id = self.orchestrator.capture_engine.uia.get_focused_control_identifier(
                        int(fg)
                    )
                    self.orchestrator.auto_monitor.on_paste_or_content_event(int(fg), ctrl_id)
        except Exception:
            return

    def _on_automatic_mode_changed(self, enabled: bool) -> None:
        try:
            self._auto_last_clipboard_seq = (
                self.orchestrator.capture_engine.inspector.get_clipboard_sequence_number()
            )
        except Exception:
            self._auto_last_clipboard_seq = 0

        self.orchestrator.settings.automatic_mode_enabled = bool(enabled)
        self.orchestrator.auto_monitor.set_idle_timeout_seconds(
            float(self.orchestrator.settings.auto_idle_timeout_seconds)
        )
        self.orchestrator.auto_monitor.set_enabled(bool(enabled))

        if hasattr(self, "auto_mode_chk") and self.auto_mode_chk.isChecked() != bool(enabled):
            self.auto_mode_chk.blockSignals(True)
            self.auto_mode_chk.setChecked(bool(enabled))
            self.auto_mode_chk.blockSignals(False)

        if enabled:
            self._auto_clipboard_timer.start()
        else:
            self._auto_clipboard_timer.stop()
            self._auto_idle_timer.stop()

        with self._auto_lock:
            self._auto_buffer = ""
        self._auto_last_hwnd = None

        timeout_s = int(self.orchestrator.settings.auto_idle_timeout_seconds)
        if enabled:
            self.status.setText(
                f"Tradução automática ativada: aguarda {timeout_s}s de inatividade para traduzir o campo integral via DeepL."
            )
            self.floating.setToolTip(f"Modo automático ativo — pausa de {timeout_s}s")
        else:
            self.status.setText("Tradução automática desativada.")
            self.floating.setToolTip("Modo automático desativado (clique no centro para ativar)")

        self._refresh_overview_cards()

    def _on_auto_mode_chk_toggled(self, checked: bool) -> None:
        if self.floating.automatic_mode != bool(checked):
            self.floating.set_automatic_mode(bool(checked), emit_signal=True)

    def _on_auto_key_event(self, key_name: str, ctrl_pressed: bool, alt_pressed: bool) -> None:
        if not self.floating.automatic_mode:
            return
        inspector = self.orchestrator.capture_engine.inspector
        hwnd = inspector.get_foreground_hwnd()
        ctx = inspector.get_window_context_for_hwnd(hwnd) if hwnd else None
        if not hwnd or (ctx and inspector.is_own_process(ctx)):
            return
        self._auto_last_hwnd = int(hwnd)
        _, ctrl_id = self.orchestrator.capture_engine.uia.get_focused_control_identifier(int(hwnd))
        self.orchestrator.auto_monitor.on_key_event(
            key_name=key_name,
            hwnd=int(hwnd),
            control_id=ctrl_id,
            ctrl_pressed=ctrl_pressed,
            alt_pressed=alt_pressed,
        )

    def _on_auto_mouse_event(self, event_kind: str) -> None:
        if not self.floating.automatic_mode:
            return
        inspector = self.orchestrator.capture_engine.inspector
        hwnd = inspector.get_foreground_hwnd()
        ctx = inspector.get_window_context_for_hwnd(hwnd) if hwnd else None
        if not hwnd or (ctx and inspector.is_own_process(ctx)):
            return
        _, ctrl_id = self.orchestrator.capture_engine.uia.get_focused_control_identifier(int(hwnd))
        self.orchestrator.auto_monitor.on_mouse_interaction(event_kind, int(hwnd), ctrl_id)

    def _capture_entire_focused_text(self, expected_hwnd: int) -> str:
        """Método auxiliar retrocompatível delegando ao motor seguro de captura integral."""
        res = self.orchestrator.capture_engine.capture_entire_field(
            target_hwnd=expected_hwnd,
            prefer_uia=self.orchestrator.settings.prefer_uia_capture,
            allow_clipboard_fallback=self.orchestrator.settings.allow_clipboard_fallback,
        )
        if not res.success:
            raise RuntimeError(res.error_message or "Falha na captura integral do campo.")
        return res.text.strip()

    def _process_auto_typing(self) -> None:
        """Disparado quando o temporizador de 5s expira sem novas interações."""
        if not self.floating.automatic_mode:
            return
        if self._auto_cycle_worker and self._auto_cycle_worker.isRunning():
            return

        if self.api.text().strip():
            self.orchestrator.settings_manager.set_provider_api_key(
                ProviderType.DEEPL, self.api.text().strip()
            )

        target_code = self.languages.get(self.language.currentText(), "EN-US")
        source_code = (
            "AUTO"
            if self.orchestrator.settings.auto_detect_source
            else self.current_source_lang_code
        )

        self.floating.set_visual_state(
            FloatingButtonState.TRANSLATING,
            "Texto concluído. Traduzindo e substituindo via DeepL...",
        )
        self._auto_cycle_worker = AutoIdleCycleWorker(
            orchestrator=self.orchestrator,
            source_lang=source_code,
            target_lang=target_code,
        )
        self._auto_cycle_worker.cycle_finished.connect(self._on_auto_cycle_finished)
        self._auto_cycle_worker.cycle_failed.connect(self._on_auto_translation_failed)
        self._auto_cycle_worker.start()

    def _on_auto_cycle_finished(self, outcome: AutoCycleOutcome) -> None:
        try:
            self._auto_last_clipboard_seq = (
                self.orchestrator.capture_engine.inspector.get_clipboard_sequence_number()
            )
        except Exception:
            pass

        if outcome.executed and outcome.translation and outcome.replacement:
            res = outcome.translation
            self.source_editor.blockSignals(True)
            self.source_editor.setPlainText(res.source_text)
            self.source_editor.blockSignals(False)
            self.target_editor.setPlainText(res.translated_text)
            origin_tag = "CACHE SQLite" if res.cached else "DeepL API"
            self.selection_preview.setText(
                f"Última tradução automática ({res.detected_source_lang} → {res.target_lang} via {origin_tag}):\n"
                f"{res.translated_text}"
            )
            self.floating.set_visual_state(
                FloatingButtonState.SUCCESS,
                f"Texto traduzido e substituído ({origin_tag})",
            )
            self.status.setText(
                f"Tradução automática concluída ({res.detected_source_lang} → {res.target_lang} via {origin_tag})."
            )
            self.refresh_all_views()
        else:
            reason = outcome.skipped_reason or "Ciclo automático ignorado."
            self.floating.set_visual_state(FloatingButtonState.IDLE, reason)
            if "bloqueada" in reason.lower() or "cancelada" in reason.lower():
                self.status.setText(f"Proteção ativa: {reason}")

    def _on_auto_translation_failed(self, error: str) -> None:
        self.floating.set_visual_state(FloatingButtonState.ERROR, f"Tradução automática: {error}")
        self.status.setText(f"Falha na tradução automática (texto original preservado): {error}")
        self.refresh_all_views()

    # -------------------------------------------------------------------------
    # FLUXOS COMPLEMENTARES DE CAPTURA E TRADUÇÃO SOB DEMANDA
    # -------------------------------------------------------------------------
    def _on_floating_panel_opened(self, hwnd: int) -> None:
        if hwnd > 0:
            self.target_hwnd = hwnd

    def _on_hotkey_capture(self, hwnd: int) -> None:
        self.schedule_capture(hwnd, auto_translate_after=False)

    def _on_hotkey_inline_translate(self, hwnd: int) -> None:
        self.schedule_capture(
            hwnd, auto_translate_after=True, direction=TranslationDirection.INLINE_REPLACE
        )

    def _on_hotkey_incoming_translate(self, hwnd: int) -> None:
        self.schedule_capture(
            hwnd, auto_translate_after=True, direction=TranslationDirection.INCOMING_RECEIVED
        )

    def schedule_capture(
        self,
        hwnd: int,
        auto_translate_after: bool = False,
        direction: TranslationDirection = TranslationDirection.OUTGOING_SENT,
    ) -> None:
        if hwnd > 0:
            self.target_hwnd = hwnd
        if hwnd > 0 and not self.floating.isVisible() and not auto_translate_after:
            return
        QTimer.singleShot(
            140,
            lambda: self._start_capture_worker(
                hwnd,
                auto_translate_after=auto_translate_after,
                direction=direction,
                from_clipboard_directly=False,
            ),
        )

    def schedule_clipboard_capture(self, hwnd: int) -> None:
        if hwnd > 0:
            self.target_hwnd = hwnd
        if hwnd > 0 and not self.floating.isVisible():
            return
        QTimer.singleShot(
            100,
            lambda: self._start_capture_worker(
                hwnd,
                auto_translate_after=False,
                direction=TranslationDirection.OUTGOING_SENT,
                from_clipboard_directly=True,
            ),
        )

    def _start_capture_worker(
        self,
        hwnd: int,
        auto_translate_after: bool = False,
        direction: TranslationDirection = TranslationDirection.OUTGOING_SENT,
        from_clipboard_directly: bool = False,
    ) -> None:
        if self.capture_worker and self.capture_worker.isRunning():
            return
        target = hwnd if hwnd > 0 else self.floating.last_external_hwnd
        self.capture_worker = CaptureWorker(
            self.orchestrator,
            target_hwnd=target,
            from_clipboard_directly=from_clipboard_directly,
        )
        self.capture_worker.captured.connect(
            lambda cap, prof: self._on_capture_completed(cap, prof, auto_translate_after, direction)
        )
        self.capture_worker.failed.connect(self._on_capture_failed)
        self.capture_worker.start()

    def _on_capture_completed(
        self,
        cap: CaptureResult,
        profile: ApplicationProfile,
        auto_translate_after: bool,
        direction: TranslationDirection,
    ) -> None:
        self.active_profile = profile
        self.profile_badge.setText(f"Perfil: {profile.app_name}")

        if not cap.success or not cap.text.strip():
            return

        self.active_capture = cap
        self.selected_text = cap.text
        self.pretranslated_text = ""
        if cap.window_context and cap.window_context.hwnd:
            self.target_hwnd = cap.window_context.hwnd

        detected_code = LanguageDetector.detect(cap.text)
        if not self.orchestrator.settings.auto_detect_source:
            self.current_source_lang_code = detected_code
            self.floating.set_source_language(detected_code)

        self.source_editor.blockSignals(True)
        self.source_editor.setPlainText(cap.text)
        self.source_editor.blockSignals(False)

        preview = cap.text.replace("\r", " ").replace("\n", " ")
        if len(preview) > 200:
            preview = preview[:200] + "..."
        self.selection_preview.setText(f"Texto detectado ({detected_code}):\n{preview}")
        self.floating.set_visual_state(
            FloatingButtonState.CAPTURED,
            f"Texto capturado ({len(cap.text)} chars).",
        )

        if auto_translate_after:
            self.translate_selection(direction=direction)
        elif self.orchestrator.settings.auto_pretranslate_on_copy:
            self._start_auto_pretranslate(cap)

    def _start_auto_pretranslate(self, cap: CaptureResult) -> None:
        target_code = self.languages.get(self.language.currentText(), "EN-US")
        provider_val = self.quick_provider_combo.currentData() or ProviderType.DEEPL.value
        provider_enum = ProviderType(provider_val)

        if self.api.text().strip():
            self.orchestrator.settings_manager.set_provider_api_key(
                ProviderType.DEEPL, self.api.text().strip()
            )

        self.pretranslate_worker = PreTranslateWorker(
            orchestrator=self.orchestrator,
            text=cap.text,
            source_lang=(
                None
                if self.orchestrator.settings.auto_detect_source
                else self.current_source_lang_code
            ),
            target_lang=target_code,
            capture_result=cap,
            preferred_provider=provider_enum,
        )
        self.pretranslate_worker.pretranslated.connect(self._on_pretranslate_ready)
        self.pretranslate_worker.start()

    def _on_pretranslate_ready(self, result: TranslationResult) -> None:
        self.pretranslated_text = result.translated_text
        self.target_editor.setPlainText(result.translated_text)
        self.status.setText(
            f"Tradução pronta ({result.detected_source_lang} → {result.target_lang})."
        )
        self.refresh_all_views()

    def _on_capture_failed(self, error: str) -> None:
        self.status.setText(f"Aviso de captura: {error}")

    def _on_source_editor_changed(self) -> None:
        self.selected_text = self.source_editor.toPlainText()

    def _on_ui_auto_detect_toggled(self, checked: bool) -> None:
        self.orchestrator.settings.auto_detect_source = bool(checked)
        self.source_language_combo.setEnabled(not bool(checked))
        self.floating.set_auto_detect(bool(checked))
        self.current_source_lang_code = (
            "AUTO"
            if checked
            else self.languages.get(self.source_language_combo.currentText(), "PT-BR")
        )
        self.orchestrator.settings_manager.save(self.orchestrator.settings)
        self._refresh_overview_cards()

    def _on_floating_auto_detect_changed(self, checked: bool) -> None:
        self.orchestrator.settings.auto_detect_source = bool(checked)
        self.auto_detect_source_chk.blockSignals(True)
        self.auto_detect_source_chk.setChecked(bool(checked))
        self.auto_detect_source_chk.blockSignals(False)
        self.source_language_combo.setEnabled(not bool(checked))
        self.current_source_lang_code = (
            "AUTO"
            if checked
            else self.languages.get(self.source_language_combo.currentText(), "PT-BR")
        )
        self.orchestrator.settings_manager.save(self.orchestrator.settings)
        self._refresh_overview_cards()

    def _on_floating_source_lang_changed(self, lang_name: str, lang_code: str) -> None:
        if lang_code == "AUTO":
            self._on_floating_auto_detect_changed(True)
            return
        self.current_source_lang_code = lang_code
        self.orchestrator.settings.auto_detect_source = False
        self.orchestrator.settings.source_language = lang_name
        self.orchestrator.settings.source_language_code = lang_code
        self.auto_detect_source_chk.blockSignals(True)
        self.auto_detect_source_chk.setChecked(False)
        self.auto_detect_source_chk.blockSignals(False)
        self.source_language_combo.setEnabled(True)
        self.source_language_combo.blockSignals(True)
        self.source_language_combo.setCurrentText(lang_name)
        self.source_language_combo.blockSignals(False)
        self.orchestrator.settings_manager.save(self.orchestrator.settings)
        self._refresh_overview_cards()

    def _on_floating_target_lang_changed(self, lang_name: str, lang_code: str) -> None:
        self.orchestrator.settings.language = lang_name
        self.orchestrator.settings.target_language_code = lang_code
        self.language.blockSignals(True)
        self.language.setCurrentText(lang_name)
        self.language.blockSignals(False)
        self.orchestrator.settings_manager.save(self.orchestrator.settings)
        self._refresh_overview_cards()

    def translate_selection(
        self,
        direction: TranslationDirection = TranslationDirection.OUTGOING_SENT,
    ) -> None:
        if self.translation_worker and self.translation_worker.isRunning():
            return

        if self.api.text().strip():
            self.orchestrator.settings_manager.set_provider_api_key(
                ProviderType.DEEPL, self.api.text().strip()
            )

        target_code = self.languages.get(self.language.currentText(), "EN-US")
        provider_val = self.quick_provider_combo.currentData() or ProviderType.DEEPL.value
        formality_val = self.quick_formality_combo.currentData() or FormalityLevel.DEFAULT.value
        provider_enum = ProviderType(provider_val)
        formality_enum = FormalityLevel(formality_val)

        resolved_hwnd = (
            self.floating.last_external_hwnd
            or self.target_hwnd
            or self.orchestrator.capture_engine.inspector.get_foreground_hwnd()
        )
        fallback_text = self.selected_text.strip() or self.source_editor.toPlainText().strip()
        src_arg = (
            "AUTO"
            if self.orchestrator.settings.auto_detect_source
            else self.current_source_lang_code
        )

        self.translate_button.setEnabled(False)
        self.floating.set_visual_state(
            FloatingButtonState.TRANSLATING, "Traduzindo e substituindo..."
        )
        self.status.setText("Capturando, traduzindo via DeepL e substituindo no campo original...")

        self.translation_worker = TranslateAndReplaceWorker(
            orchestrator=self.orchestrator,
            target_hwnd=resolved_hwnd,
            fallback_text=fallback_text,
            source_lang=src_arg,
            target_lang=target_code,
            direction=direction,
            existing_capture=self.active_capture,
            auto_replace=True,
            preferred_provider=provider_enum,
            formality=formality_enum,
        )
        self.translation_worker.completed.connect(self._on_translation_completed)
        self.translation_worker.failed.connect(self._on_translation_failed)
        self.translation_worker.start()

    def translate_incoming_selection(self) -> None:
        self.translate_selection(direction=TranslationDirection.INCOMING_RECEIVED)

    def cancel_active_translation(self) -> None:
        if self.translation_worker and self.translation_worker.isRunning():
            self.translation_worker.cancel()
            self.translate_button.setEnabled(True)
            self.floating.set_visual_state(FloatingButtonState.IDLE, "Operação cancelada")
            self.status.setText("Operação cancelada.")

    def _on_translation_completed(
        self,
        capture: CaptureResult,
        result: TranslationResult,
        replacement: ReplacementResult | None,
    ) -> None:
        self.translate_button.setEnabled(True)
        self.active_capture = capture
        self.source_editor.blockSignals(True)
        self.source_editor.setPlainText(capture.text)
        self.source_editor.blockSignals(False)
        self.target_editor.setPlainText(result.translated_text)
        self.floating.action_panel.hide()

        origin_label = "CACHE SQLite" if result.cached else "DeepL API"
        if replacement is not None and replacement.success:
            self.selected_text = result.translated_text
            self.floating.set_visual_state(
                FloatingButtonState.SUCCESS,
                f"Tradução inserida no campo original ({origin_label})!",
            )
            self.selection_preview.setText(
                f"Última tradução ({result.detected_source_lang} → {result.target_lang} via {origin_label}):\n"
                f"{result.translated_text}"
            )
            self.status.setText(replacement.message)
        else:
            self._copy_to_clipboard(result.translated_text)
            msg = replacement.message if replacement else "Tradução concluída."
            self.floating.set_visual_state(FloatingButtonState.WARNING, msg)
            self.selection_preview.setText(
                f"Tradução concluída ({result.detected_source_lang} → {result.target_lang} via {origin_label}):\n"
                f"{result.translated_text}"
            )
            self.status.setText(f"{msg}")

        self.refresh_all_views()

    def _on_translation_failed(self, error_msg: str) -> None:
        self.translate_button.setEnabled(True)
        self.floating.set_visual_state(FloatingButtonState.ERROR, error_msg)
        self.status.setText(f"Falha na tradução: {error_msg}")
        self.refresh_all_views()

    def _replace_from_popup(self, edited_translation: str) -> None:
        if not self.active_capture or not self.active_capture.window_context:
            self._copy_to_clipboard(edited_translation)
            return
        rep = self.orchestrator.capture_engine.replace_text_safely(
            new_text=edited_translation,
            original_context=self.active_capture.window_context,
            expected_original_text=self.active_capture.text,
            verify_content_before_paste=False,
        )
        self.status.setText(rep.message)

    def rollback_last_action(self) -> None:
        rep = self.orchestrator.rollback_last_replacement()
        self.status.setText(rep.message)

    def _on_quick_source_language_changed(self, lang_name: str) -> None:
        if lang_name in self.languages:
            code = self.languages[lang_name]
            self.current_source_lang_code = code
            self.orchestrator.settings.source_language = lang_name
            self.orchestrator.settings.source_language_code = code
            if not self.orchestrator.settings.auto_detect_source:
                self.floating.set_source_language(lang_name)
            self._refresh_overview_cards()

    def _on_quick_language_changed(self, lang_name: str) -> None:
        if lang_name in self.languages:
            code = self.languages[lang_name]
            self.orchestrator.settings.language = lang_name
            self.orchestrator.settings.target_language_code = code
            self.floating.set_target_language(lang_name)
            self._refresh_overview_cards()

    def _on_floating_closed_by_user(self) -> None:
        """Acionado ao clicar no X do botão flutuante: interrompe monitoramento e oculta o botão."""
        self.orchestrator.auto_monitor.close_and_stop()
        self._auto_idle_timer.stop()
        self._auto_clipboard_timer.stop()
        with self._auto_lock:
            self._auto_buffer = ""
        self.orchestrator.settings.show_floating_button = False
        self.orchestrator.settings.automatic_mode_enabled = False
        self.orchestrator.settings_manager.save(self.orchestrator.settings)
        self.float_toggle_btn.setText("Exibir Botão Flutuante")
        self.status.setText(
            "Botão flutuante fechado e monitoramento automático interrompido."
        )
        self._refresh_overview_cards()

    def _on_floating_moved(self, x: int, y: int) -> None:
        self.orchestrator.settings.floating_pos_x = x
        self.orchestrator.settings.floating_pos_y = y
        self.orchestrator.settings_manager.save(self.orchestrator.settings)

    # -------------------------------------------------------------------------
    # INTEGRAÇÃO DEEPL (/v2/usage) E CONTROLE DE CONSUMO
    # -------------------------------------------------------------------------
    def test_deepl_connection(self) -> None:
        key = self.api.text().strip()
        if key:
            self.orchestrator.settings_manager.set_provider_api_key(ProviderType.DEEPL, key)
        self.orchestrator.settings.deepl_account_type = str(
            self.deepl_account_type_combo.currentData() or DeepLAccountType.AUTO.value
        )
        self.deepl_test_button.setEnabled(False)
        self.deepl_usage_btn.setEnabled(False)
        self.deepl_auth_status_label.setText("Status de autenticação: verificando junto à DeepL...")
        self.deepl_usage_label.setText("Consultando endpoint oficial /v2/usage...")
        self._deepl_usage_worker = DeepLUsageWorker(self.orchestrator)
        self._deepl_usage_worker.succeeded.connect(self._on_deepl_usage_success)
        self._deepl_usage_worker.failed.connect(self._on_deepl_usage_failure)
        self._deepl_usage_worker.finished.connect(self._on_deepl_usage_worker_done)
        self._deepl_usage_worker.start()

    def _on_deepl_usage_worker_done(self) -> None:
        self.deepl_test_button.setEnabled(True)
        self.deepl_usage_btn.setEnabled(True)

    def _on_deepl_usage_success(self, usage: dict) -> None:
        used = int(usage.get("character_count", 0))
        limit = int(usage.get("character_limit", 0))
        remaining = max(0, limit - used) if limit else 0
        tier = str(usage.get("account_tier", "auto")).upper()
        local = self.orchestrator.get_local_usage_summary()
        percent = (used / limit * 100.0) if limit else 0.0
        threshold = int(self.orchestrator.settings.deepl_alert_threshold_percent)
        alert = (
            f" ALERTA: consumo atingiu {percent:.1f}% (limite de alerta: {threshold}%)."
            if limit and percent >= threshold
            else ""
        )
        self.deepl_auth_status_label.setText(
            f"Status de autenticação: Autenticado com sucesso (Conta DeepL {tier})"
        )
        self.deepl_auth_status_label.setStyleSheet("font-weight: 700; color: #16A36A;")
        self.deepl_usage_label.setText(
            f"DeepL conectada. Uso confirmado na conta: {used:,} / {limit:,} caracteres "
            f"({percent:.1f}%); saldo conforme API: {remaining:,}. Controle local estimado: "
            f"{local['estimated_characters']:,}; confirmado em respostas: {local['confirmed_characters']:,}.{alert}"
        )
        self.refresh_consumption_view()
        self._refresh_overview_cards()

    def _on_deepl_usage_failure(self, error: str) -> None:
        self.deepl_auth_status_label.setText("Status de autenticação: Falha na autenticação/conexão")
        self.deepl_auth_status_label.setStyleSheet("font-weight: 700; color: #D92D20;")
        self.deepl_usage_label.setText(f"Falha ao consultar DeepL: {error}")
        self._refresh_overview_cards()

    # -------------------------------------------------------------------------
    # PERSISTÊNCIA DE CONFIGURAÇÕES E ATUALIZAÇÃO DAS 6 SEÇÕES
    # -------------------------------------------------------------------------
    def save(self) -> None:
        s = self.orchestrator.settings
        s.auto_detect_source = self.auto_detect_source_chk.isChecked()
        s.source_language = self.source_language_combo.currentText()
        s.source_language_code = self.languages.get(s.source_language, "PT-BR")
        s.language = self.language.currentText()
        s.target_language_code = self.languages.get(s.language, "EN-US")
        s.incoming_language = self.incoming_language_combo.currentText()
        s.incoming_language_code = self.languages.get(s.incoming_language, "PT-BR")
        s.active_provider = str(self.quick_provider_combo.currentData() or ProviderType.DEEPL.value)
        s.deepl_account_type = str(
            self.deepl_account_type_combo.currentData() or DeepLAccountType.AUTO.value
        )
        s.formality = str(self.quick_formality_combo.currentData() or FormalityLevel.DEFAULT.value)
        s.show_floating_button = self.floating.isVisible()
        s.automatic_mode_enabled = self.floating.automatic_mode
        s.capture_hotkey = self.capture_hotkey_input.text().strip() or "ctrl+shift+c"
        s.inline_translate_hotkey = self.inline_hotkey_input.text().strip() or "ctrl+shift+t"
        s.incoming_translate_hotkey = self.incoming_hotkey_input.text().strip() or "ctrl+shift+r"
        s.request_timeout_seconds = float(self.timeout_spin.value())
        s.auto_idle_timeout_seconds = int(self.idle_timeout_spin.value())
        s.deepl_alert_threshold_percent = int(self.deepl_alert_spin.value())
        self._auto_idle_timer.setInterval(s.auto_idle_timeout_seconds * 1000)
        self.orchestrator.auto_monitor.set_idle_timeout_seconds(float(s.auto_idle_timeout_seconds))
        s.prefer_uia_capture = self.prefer_uia_chk.isChecked()
        s.verify_before_replace = self.verify_replace_chk.isChecked()
        s.protect_sensitive_fields = self.protect_sensitive_chk.isChecked()
        s.enable_legacy_ctrl_a_hook = self.legacy_ctrl_a_chk.isChecked()
        s.privacy_no_history = self.privacy_mode_chk.isChecked()
        s.start_with_windows = self.start_with_windows_chk.isChecked()

        self.orchestrator.startup_manager.set_enabled(s.start_with_windows)

        self.floating.set_auto_detect(s.auto_detect_source)
        self.floating.set_source_language("AUTO" if s.auto_detect_source else s.source_language)
        self.floating.set_target_language(s.language)

        self.orchestrator.settings_manager.set_provider_api_key(
            ProviderType.DEEPL, self.api.text().strip()
        )
        self.orchestrator.settings_manager.set_provider_api_key(
            ProviderType.GOOGLE, self.google_api_input.text().strip()
        )
        self.orchestrator.settings_manager.set_provider_api_key(
            ProviderType.MICROSOFT, self.ms_api_input.text().strip()
        )
        self.orchestrator.settings_manager.set_provider_api_key(
            ProviderType.OPENAI, self.openai_api_input.text().strip()
        )

        self.orchestrator.settings_manager.save(s)
        self.register_all_hotkeys()
        self.refresh_all_views()
        self.status.setText("Configurações e credenciais salvas com segurança.")

    def toggle_float(self) -> None:
        if self.floating.isVisible():
            self.floating.close_floating_button()
        else:
            self.floating.show()
            self.float_toggle_btn.setText("Ocultar Botão Flutuante")
            self.orchestrator.settings.show_floating_button = True
            self.orchestrator.settings_manager.save(self.orchestrator.settings)
            self._refresh_overview_cards()

    def _on_tab_changed(self, index: int) -> None:
        self.refresh_all_views()

    def refresh_all_views(self) -> None:
        self._refresh_overview_cards()
        self.refresh_profiles_table()
        self.refresh_glossary_table()
        self.refresh_history_table()
        self.refresh_consumption_view()
        self.refresh_metrics_table()
        self.refresh_diagnostics_table()

    def _refresh_overview_cards(self) -> None:
        auto_on = self.floating.automatic_mode
        timeout_s = int(self.orchestrator.settings.auto_idle_timeout_seconds)
        if auto_on:
            self.auto_state_badge.setText(f"Modo Automático: ATIVO ({timeout_s}s)")
            self.auto_state_badge.setStyleSheet(
                "background-color: #16A36A; color: #FFFFFF; padding: 5px 12px; border-radius: 11px; font-weight: 700;"
            )
            self.overview_state_lbl.setText(f"Monitorando digitação ({timeout_s}s de inatividade)")
            self.overview_state_lbl.setStyleSheet("font-size: 15px; font-weight: 700; color: #16A36A;")
            self.overview_toggle_auto_btn.setText("Desativar Tradução Automática")
            self.overview_toggle_auto_btn.setObjectName("DangerButton")
        else:
            self.auto_state_badge.setText("Modo Automático: Inativo")
            self.auto_state_badge.setStyleSheet(
                "background-color: #344054; color: #F2F4F7; padding: 5px 12px; border-radius: 11px; font-weight: 700;"
            )
            self.overview_state_lbl.setText("Inativo (Clique no botão flutuante para ativar)")
            self.overview_state_lbl.setStyleSheet("font-size: 15px; font-weight: 700; color: #D0D5DD;")
            self.overview_toggle_auto_btn.setText(f"Ativar Tradução Automática ({timeout_s}s)")
            self.overview_toggle_auto_btn.setObjectName("SuccessButton")

        src_disp = (
            "Detecção Automática"
            if self.orchestrator.settings.auto_detect_source
            else self.orchestrator.settings.source_language
        )
        tgt_disp = self.orchestrator.settings.language
        self.overview_langs_lbl.setText(f"Origem: {src_disp}\nDestino: {tgt_disp}")

        has_deepl_key = bool(
            (self.api.text().strip() if hasattr(self, "api") else "")
            or self.orchestrator.settings_manager.get_provider_api_key(ProviderType.DEEPL)
        )
        snap = self.orchestrator.usage_tracker.get_latest_api_snapshot()
        if has_deepl_key and snap:
            rem = int(snap.get("character_remaining", 0))
            self.overview_deepl_lbl.setText(
                f"Chave configurada ({str(snap.get('account_tier', 'auto')).upper()})\n"
                f"Saldo API: {rem:,} caracteres disponíveis"
            )
        elif has_deepl_key:
            self.overview_deepl_lbl.setText(
                "Chave API armazenada no Windows Credential Manager\n(Clique em Testar Conexão para consultar saldo)"
            )
        else:
            self.overview_deepl_lbl.setText(
                "Chave DeepL ainda não configurada\n(Acesse a aba 3. DeepL para cadastrar)"
            )

    def refresh_consumption_view(self) -> None:
        summary = self.orchestrator.get_local_usage_summary()
        snap = self.orchestrator.usage_tracker.get_latest_api_snapshot()
        threshold = int(self.orchestrator.settings.deepl_alert_threshold_percent)

        if snap and snap["character_limit"] > 0:
            used = int(snap["character_count"])
            limit = int(snap["character_limit"])
            rem = int(snap["character_remaining"])
            pct = (used / limit) * 100.0
            self.consumption_api_used_lbl.setText(
                f"Caracteres utilizados (API DeepL): {used:,} ({pct:.1f}%)"
            )
            self.consumption_api_limit_lbl.setText(f"Limite informado pela API: {limit:,}")
            self.consumption_api_remaining_lbl.setText(f"Saldo disponível (API DeepL): {rem:,}")
            if pct >= threshold:
                self.consumption_alert_banner.setText(
                    f"ALERTA DE CONSUMO: Você utilizou {pct:.1f}% do limite informado pela DeepL "
                    f"(alerta configurado em {threshold}%)."
                )
                self.consumption_alert_banner.setStyleSheet(
                    "background-color: #B42318; color: #FFFFFF; padding: 8px; border-radius: 6px; font-weight: 700;"
                )
            else:
                self.consumption_alert_banner.setText(
                    f"Consumo dentro do limite seguro ({pct:.1f}% utilizado; alerta em {threshold}%)."
                )
                self.consumption_alert_banner.setStyleSheet("color: #16A36A; font-weight: 600;")
        else:
            self.consumption_api_used_lbl.setText("Caracteres utilizados (API DeepL): não consultado")
            self.consumption_api_limit_lbl.setText("Limite informado pela API: não consultado")
            self.consumption_api_remaining_lbl.setText("Saldo disponível (API DeepL): não consultado")

        self.consumption_local_confirmed_lbl.setText(
            f"Confirmados em respostas locais (/v2/translate): {summary['confirmed_characters']:,}"
        )
        self.consumption_local_estimated_lbl.setText(
            f"Estimativa local não confirmada: {summary['estimated_characters']:,}"
        )
        self.consumption_cache_saved_lbl.setText(
            f"Economizados via Cache SQLite: {summary.get('saved_by_cache_characters', 0):,}"
        )

        events = self.orchestrator.usage_tracker.list_events(limit=60)
        self.usage_events_table.setRowCount(len(events))
        for idx, ev in enumerate(events):
            dt = datetime.datetime.fromtimestamp(ev["created_at"]).strftime("%d/%m %H:%M:%S")
            if ev["cached"]:
                kind = "ECONOMIZADO (Cache SQLite)"
            elif ev["confirmed"]:
                kind = "CONFIRMADO (DeepL API)"
            else:
                kind = "ESTIMATIVA LOCAL"
            pair = (
                f"{ev['source_lang']} → {ev['target_lang']}"
                if (ev["source_lang"] or ev["target_lang"])
                else "N/A"
            )
            self.usage_events_table.setItem(idx, 0, QTableWidgetItem(dt))
            self.usage_events_table.setItem(idx, 1, QTableWidgetItem(f"{ev['characters']:,}"))
            self.usage_events_table.setItem(idx, 2, QTableWidgetItem(kind))
            self.usage_events_table.setItem(idx, 3, QTableWidgetItem(ev["source"]))
            self.usage_events_table.setItem(idx, 4, QTableWidgetItem(pair))

    def refresh_profiles_table(self) -> None:
        profiles = self.orchestrator.repository.list_profiles()
        self.profiles_table.setRowCount(len(profiles))
        for row_idx, p in enumerate(profiles):
            self.profiles_table.setItem(row_idx, 0, QTableWidgetItem(p.app_name))
            self.profiles_table.setItem(row_idx, 1, QTableWidgetItem(", ".join(p.process_names)))
            self.profiles_table.setItem(row_idx, 2, QTableWidgetItem(", ".join(p.window_title_patterns)))
            self.profiles_table.setItem(row_idx, 3, QTableWidgetItem(p.target_lang))
            self.profiles_table.setItem(row_idx, 4, QTableWidgetItem(p.incoming_target_lang))
            self.profiles_table.setItem(row_idx, 5, QTableWidgetItem(p.formality.value))
            self.profiles_table.setItem(
                row_idx, 6, QTableWidgetItem("Sim" if p.review_before_replace else "Não")
            )

    def _save_profile_from_ui(self) -> None:
        name = self.prof_name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Perfil Inválido", "Informe o nome do perfil de aplicativo.")
            return
        procs = [x.strip() for x in self.prof_proc_input.text().split(",") if x.strip()]
        titles = [x.strip() for x in self.prof_title_input.text().split(",") if x.strip()]
        target_code = str(self.prof_target_combo.currentData() or "EN-US")
        formality_val = str(self.prof_formality_combo.currentData() or FormalityLevel.DEFAULT.value)

        prof = ApplicationProfile(
            app_name=name,
            process_names=procs,
            window_title_patterns=titles,
            source_lang="PT-BR",
            target_lang=target_code,
            incoming_target_lang="PT-BR",
            formality=FormalityLevel(formality_val),
            review_before_replace=self.prof_review_chk.isChecked(),
        )
        self.orchestrator.repository.upsert_profile(prof)
        self.orchestrator.reload_domain_rules()
        self.refresh_profiles_table()
        self.status.setText(f"Perfil '{name}' salvo com sucesso.")

    def refresh_glossary_table(self) -> None:
        entries = self.orchestrator.repository.list_glossary()
        self.glossary_table.setRowCount(len(entries))
        for row_idx, e in enumerate(entries):
            self.glossary_table.setItem(row_idx, 0, QTableWidgetItem(str(e.id or "")))
            self.glossary_table.setItem(row_idx, 1, QTableWidgetItem(e.source_term))
            self.glossary_table.setItem(row_idx, 2, QTableWidgetItem(e.target_term))
            self.glossary_table.setItem(row_idx, 3, QTableWidgetItem(f"{e.source_lang} → {e.target_lang}"))
            self.glossary_table.setItem(
                row_idx, 4, QTableWidgetItem("Sim" if e.do_not_translate else "Não")
            )
            self.glossary_table.setItem(row_idx, 5, QTableWidgetItem(e.domain_tag))

    def _add_glossary_from_ui(self) -> None:
        src = self.gloss_src_input.text().strip()
        tgt = self.gloss_tgt_input.text().strip()
        if not src:
            QMessageBox.warning(self, "Glossário", "Informe o termo original.")
            return
        if not tgt:
            tgt = src
        entry = GlossaryEntry(
            source_term=src,
            target_term=tgt,
            do_not_translate=self.gloss_dnt_chk.isChecked(),
            case_sensitive=self.gloss_case_chk.isChecked(),
            domain_tag=self.gloss_domain_input.text().strip() or "geral",
        )
        self.orchestrator.repository.upsert_glossary_entry(entry)
        self.orchestrator.reload_domain_rules()
        self.gloss_src_input.clear()
        self.gloss_tgt_input.clear()
        self.refresh_glossary_table()
        self.status.setText(f"Termo '{src}' adicionado ao glossário.")

    def _delete_selected_glossary(self) -> None:
        row = self.glossary_table.currentRow()
        if row < 0:
            return
        item = self.glossary_table.item(row, 0)
        if item and item.text().isdigit():
            self.orchestrator.repository.delete_glossary_entry(int(item.text()))
            self.orchestrator.reload_domain_rules()
            self.refresh_glossary_table()

    def refresh_history_table(self) -> None:
        q = self.history_search_input.text().strip() if hasattr(self, "history_search_input") else ""
        items = self.orchestrator.repository.list_history(limit=100, search_query=q)
        self.history_table.setRowCount(len(items))
        for row_idx, h in enumerate(items):
            dt = datetime.datetime.fromtimestamp(h.created_at).strftime("%d/%m/%Y %H:%M:%S")
            self.history_table.setItem(row_idx, 0, QTableWidgetItem(dt))
            self.history_table.setItem(row_idx, 1, QTableWidgetItem(h.app_name))
            self.history_table.setItem(row_idx, 2, QTableWidgetItem(h.direction))
            self.history_table.setItem(row_idx, 3, QTableWidgetItem(f"{h.source_lang} → {h.target_lang}"))
            self.history_table.setItem(row_idx, 4, QTableWidgetItem(h.source_text))
            self.history_table.setItem(row_idx, 5, QTableWidgetItem(h.translated_text))
            prov_badge = (
                f"CACHE ({h.provider.upper()})"
                if h.cached
                else f"API ({h.provider.upper()} • {h.latency_ms:.0f}ms)"
            )
            self.history_table.setItem(row_idx, 6, QTableWidgetItem(prov_badge))

    def _on_history_row_selected(self) -> None:
        row = self.history_table.currentRow()
        if row < 0:
            return
        orig_item = self.history_table.item(row, 4)
        trans_item = self.history_table.item(row, 5)
        if orig_item and hasattr(self, "history_orig_detail"):
            self.history_orig_detail.setPlainText(orig_item.text())
        if trans_item and hasattr(self, "history_trans_detail"):
            self.history_trans_detail.setPlainText(trans_item.text())

    def _on_privacy_mode_toggled(self, checked: bool) -> None:
        self.orchestrator.settings.privacy_no_history = checked
        self.orchestrator.settings_manager.save(self.orchestrator.settings)

    def _copy_selected_history(self) -> None:
        row = self.history_table.currentRow()
        if row < 0:
            return
        item = self.history_table.item(row, 5)
        if item:
            self._copy_to_clipboard(item.text())

    def _purge_history(self) -> None:
        removed = self.orchestrator.repository.clear_history()
        self.refresh_history_table()
        self.status.setText(f"Histórico excluído permanentemente ({removed} registros removidos).")

    def refresh_metrics_table(self) -> None:
        metrics = self.orchestrator.get_provider_metrics()
        self.metrics_table.setRowCount(len(metrics))
        for row_idx, m in enumerate(metrics):
            self.metrics_table.setItem(row_idx, 0, QTableWidgetItem(m.provider.upper()))
            self.metrics_table.setItem(row_idx, 1, QTableWidgetItem(m.circuit_state))
            self.metrics_table.setItem(row_idx, 2, QTableWidgetItem(str(m.total_requests)))
            self.metrics_table.setItem(row_idx, 3, QTableWidgetItem(str(m.cache_hits)))
            self.metrics_table.setItem(row_idx, 4, QTableWidgetItem(str(m.failed_requests)))
            self.metrics_table.setItem(row_idx, 5, QTableWidgetItem(str(m.total_characters)))
            self.metrics_table.setItem(row_idx, 6, QTableWidgetItem(f"${m.estimated_cost_usd:.4f}"))

    def refresh_diagnostics_table(self) -> None:
        cat = self.diag_category_combo.currentText() if hasattr(self, "diag_category_combo") else "TODOS"
        records = self.orchestrator.logger.get_recent_records(category=cat, limit=150)
        self.diagnostics_table.setRowCount(len(records))
        for row_idx, r in enumerate(reversed(records)):
            dt = datetime.datetime.fromtimestamp(r.timestamp).strftime("%H:%M:%S")
            self.diagnostics_table.setItem(row_idx, 0, QTableWidgetItem(dt))
            self.diagnostics_table.setItem(row_idx, 1, QTableWidgetItem(r.level))
            self.diagnostics_table.setItem(row_idx, 2, QTableWidgetItem(r.category))
            self.diagnostics_table.setItem(row_idx, 3, QTableWidgetItem(r.operation_id))
            self.diagnostics_table.setItem(row_idx, 4, QTableWidgetItem(r.message))

    def run_self_diagnostics(self) -> None:
        fg = self.orchestrator.capture_engine.inspector.get_foreground_window_context()
        summary = self.orchestrator.repository.get_usage_summary()
        self.orchestrator.logger.info(
            LogCategory.APPLICATION,
            f"Autodiagnóstico concluído: Banco SQLite OK ({summary['cache_entries']} entradas em cache), "
            f"Cofre={self.orchestrator.settings_manager.credentials.backend_name}, "
            f"Janela Ativa={fg.process_name if fg else 'N/A'}.",
        )
        self.refresh_diagnostics_table()
        self.status.setText("Autodiagnóstico concluído sem erros.")

    def export_logs_dialog(self) -> None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar Logs de Diagnóstico",
            str(Path.home() / "universal_translator_diagnostics.log"),
            "Log Files (*.log *.txt)",
        )
        if file_path:
            out = self.orchestrator.logger.export_logs(Path(file_path))
            self.status.setText(f"Logs exportados com segurança para: {out}")

    def _copy_to_clipboard(self, text: str) -> None:
        if not text:
            return
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(text, QClipboard.Mode.Clipboard)
        self.status.setText("Tradução copiada para a área de transferência.")

    def closeEvent(self, event: Any) -> None:
        if self.orchestrator.settings.minimize_to_tray and not self.closing:
            event.ignore()
            self.hide()
        else:
            super().closeEvent(event)

    def quit_app(self) -> None:
        self.closing = True
        self.cleanup()
        QApplication.quit()

    def cleanup(self) -> None:
        self.orchestrator.auto_monitor.close_and_stop()
        self.hotkey_service.unregister_all()
        try:
            if keyboard is not None and self._keyboard_hook is not None:
                keyboard.unhook(self._keyboard_hook)
            if mouse is not None and self._mouse_hook is not None:
                mouse.unhook(self._mouse_hook)
        except Exception:
            pass
        self._auto_idle_timer.stop()
        self._auto_clipboard_timer.stop()
        for worker in (
            self._auto_cycle_worker,
            self._auto_capture_worker,
            self._auto_worker,
            self.capture_worker,
            self.pretranslate_worker,
            self.translation_worker,
            self._deepl_usage_worker,
        ):
            if worker is not None and worker.isRunning():
                worker.wait(1500)
        self.floating.hide()
        self.popup_card.hide()
        self.tray.hide()
