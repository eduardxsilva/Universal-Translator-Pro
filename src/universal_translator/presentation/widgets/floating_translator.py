"""Floating Translator minimalista sem roubo de foco com três áreas interativas e painel rápido de idiomas."""
from __future__ import annotations

import ctypes
from typing import Any

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...domain.enums import (
    LANGUAGE_CODE_TO_NAME,
    SOURCE_LANGUAGES,
    SUPPORTED_LANGUAGES,
    FloatingButtonState,
)
from ...infrastructure.windows.win32_inspector import Win32WindowInspector


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_ulong),
        ("pt", _POINT),
    ]


class _NoActivateWindowMixin:
    """Garante em nível Win32 (WS_EX_NOACTIVATE + WM_MOUSEACTIVATE) que clicar no widget nunca roube o foco."""

    _inspector: Win32WindowInspector

    def _apply_win32_no_activate(self) -> None:
        try:
            hwnd = int(self.winId())  # type: ignore[attr-defined]
            self._inspector.apply_no_activate_style(hwnd)
        except Exception:
            pass

    def _handle_native_mouse_activate(self, message: Any) -> tuple[bool, int] | None:
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
            if msg.message == 0x0021:  # WM_MOUSEACTIVATE -> MA_NOACTIVATE (3)
                return True, 3
        except Exception:
            pass
        return None


class SimpleFloatingActionPanel(QWidget, _NoActivateWindowMixin):
    """Painel rápido de idiomas aberto ao clicar no ícone superior esquerdo do botão flutuante.

    Contém conforme Seção 2.2:
    1. Idioma de origem (`source_lang_button`)
    2. Idioma de destino (`target_lang_button`)
    3. Detecção automática (`auto_detect_checkbox`)
    4. Acesso às configurações de idiomas (`open_language_settings_button`)
    5. Traduzir e Substituir sob demanda (`translate_replace_button`)
    """

    source_language_selected = Signal(str, str)  # (nome_exibicao, codigo)
    target_language_selected = Signal(str, str)  # (nome_exibicao, codigo)
    auto_detect_toggled = Signal(bool)
    open_language_settings_requested = Signal()
    translate_and_replace_clicked = Signal()
    close_floating_requested = Signal()

    def __init__(
        self,
        source_lang_name: str = "Português (Brasil)",
        target_lang_name: str = "English (EUA)",
        auto_detect: bool = True,
        inspector: Win32WindowInspector | None = None,
    ) -> None:
        super().__init__(None)
        self._inspector = inspector or Win32WindowInspector()
        self.source_languages = dict(SOURCE_LANGUAGES)
        self.languages = dict(SUPPORTED_LANGUAGES)
        self.source_lang_name = source_lang_name
        self.target_lang_name = target_lang_name
        self.auto_detect = bool(auto_detect)

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedWidth(300)
        self.setStyleSheet(
            """
            QWidget {
                background-color: #101828;
                color: #F2F4F7;
                border: 1px solid #344054;
                border-radius: 10px;
                font-family: 'Inter', 'Segoe UI', Arial, sans-serif;
                font-size: 13px;
            }
            QPushButton#LangButton {
                background-color: #1D2939;
                color: #F2F4F7;
                border: 1px solid #344054;
                border-radius: 7px;
                padding: 8px 10px;
                text-align: left;
                font-weight: 600;
            }
            QPushButton#LangButton:hover {
                border-color: #2563EB;
                background-color: #26354A;
            }
            QPushButton#SettingsLinkButton {
                background-color: #1D2939;
                color: #93C5FD;
                border: 1px solid #344054;
                border-radius: 7px;
                padding: 7px 10px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton#SettingsLinkButton:hover {
                border-color: #2563EB;
                background-color: #26354A;
                color: #FFFFFF;
            }
            QPushButton#TranslateReplaceButton {
                background-color: #16A36A;
                color: #FFFFFF;
                border: none;
                border-radius: 7px;
                padding: 9px 12px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#TranslateReplaceButton:hover {
                background-color: #128456;
            }
            QPushButton#TranslateReplaceButton:disabled {
                background-color: #F79009;
                color: #FFFFFF;
            }
            QCheckBox {
                color: #D0D5DD;
                font-size: 12px;
                font-weight: 600;
                border: none;
                padding: 2px 4px;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 1. Detecção Automática
        self.auto_detect_checkbox = QCheckBox("Detecção automática de idioma")
        self.auto_detect_checkbox.setChecked(self.auto_detect)
        self.auto_detect_checkbox.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.auto_detect_checkbox.toggled.connect(self._on_auto_detect_toggled)
        layout.addWidget(self.auto_detect_checkbox)

        # 2. Botão de Idioma de Origem
        self.source_lang_button = QPushButton()
        self.source_lang_button.setObjectName("LangButton")
        self.source_lang_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.source_lang_button.clicked.connect(self._open_source_language_menu)
        layout.addWidget(self.source_lang_button)

        # 3. Botão de Idioma de Destino
        self.target_lang_button = QPushButton()
        self.target_lang_button.setObjectName("LangButton")
        self.target_lang_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.target_lang_button.clicked.connect(self._open_target_language_menu)
        layout.addWidget(self.target_lang_button)

        # 4. Botão de Traduzir e Substituir sob demanda
        self.translate_replace_button = QPushButton("Traduzir e Substituir Agora")
        self.translate_replace_button.setObjectName("TranslateReplaceButton")
        self.translate_replace_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.translate_replace_button.clicked.connect(self._on_translate_clicked)
        layout.addWidget(self.translate_replace_button)

        # 5. Acesso às configurações de idiomas
        self.open_language_settings_button = QPushButton("⚙ Configurações de Idiomas")
        self.open_language_settings_button.setObjectName("SettingsLinkButton")
        self.open_language_settings_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.open_language_settings_button.clicked.connect(self._on_open_settings_clicked)
        layout.addWidget(self.open_language_settings_button)

        self._refresh_labels()

    def _refresh_labels(self) -> None:
        src_display = "Detecção Automática" if self.auto_detect else self.source_lang_name
        self.source_lang_button.setText(f"Origem: {src_display} ▾")
        self.target_lang_button.setText(f"Destino: {self.target_lang_name} ▾")

    def set_auto_detect(self, enabled: bool) -> None:
        self.auto_detect = bool(enabled)
        self.auto_detect_checkbox.blockSignals(True)
        self.auto_detect_checkbox.setChecked(self.auto_detect)
        self.auto_detect_checkbox.blockSignals(False)
        self._refresh_labels()

    def set_source_language_by_code_or_name(self, code_or_name: str) -> None:
        val = (code_or_name or "").strip()
        if val.upper() == "AUTO" or val == "Detecção Automática":
            self.set_auto_detect(True)
            return
        if val in self.languages:
            self.source_lang_name = val
        else:
            upper = val.upper()
            if upper in LANGUAGE_CODE_TO_NAME:
                self.source_lang_name = LANGUAGE_CODE_TO_NAME[upper]
            else:
                for code, name in LANGUAGE_CODE_TO_NAME.items():
                    if code.split("-")[0] == upper.split("-")[0] and code != "AUTO":
                        self.source_lang_name = name
                        break
        self._refresh_labels()

    def set_target_language_by_code_or_name(self, code_or_name: str) -> None:
        val = (code_or_name or "").strip()
        if val in self.languages:
            self.target_lang_name = val
        elif val.upper() in LANGUAGE_CODE_TO_NAME and val.upper() != "AUTO":
            self.target_lang_name = LANGUAGE_CODE_TO_NAME[val.upper()]
        self._refresh_labels()

    def _on_auto_detect_toggled(self, checked: bool) -> None:
        self.auto_detect = bool(checked)
        self._refresh_labels()
        self.auto_detect_toggled.emit(self.auto_detect)
        if self.auto_detect:
            self.source_language_selected.emit("Detecção Automática", "AUTO")
        else:
            code = self.languages.get(self.source_lang_name, "PT-BR")
            self.source_language_selected.emit(self.source_lang_name, code)

    def _open_source_language_menu(self) -> None:
        menu = self._create_styled_menu()
        for name, code in self.source_languages.items():
            act = QAction(name, self)
            act.triggered.connect(
                lambda checked=False, n=name, c=code: self._on_source_chosen(n, c)
            )
            menu.addAction(act)
        menu.exec(self.source_lang_button.mapToGlobal(QPoint(0, self.source_lang_button.height())))

    def _open_target_language_menu(self) -> None:
        menu = self._create_styled_menu()
        for name, code in self.languages.items():
            act = QAction(name, self)
            act.triggered.connect(
                lambda checked=False, n=name, c=code: self._on_target_chosen(n, c)
            )
            menu.addAction(act)
        menu.exec(self.target_lang_button.mapToGlobal(QPoint(0, self.target_lang_button.height())))

    def _create_styled_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setWindowFlags(
            menu.windowFlags()
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        menu.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        menu.setStyleSheet(
            """
            QMenu {
                background-color: #1D2939;
                color: #F2F4F7;
                border: 1px solid #344054;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 14px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #2563EB;
                color: #FFFFFF;
            }
            """
        )
        return menu

    def _on_source_chosen(self, name: str, code: str) -> None:
        if code == "AUTO":
            self.set_auto_detect(True)
            self.auto_detect_toggled.emit(True)
        else:
            self.source_lang_name = name
            self.set_auto_detect(False)
            self.auto_detect_toggled.emit(False)
        self._refresh_labels()
        self.source_language_selected.emit(name, code)

    def _on_target_chosen(self, name: str, code: str) -> None:
        self.target_lang_name = name
        self._refresh_labels()
        self.target_language_selected.emit(name, code)

    def _on_translate_clicked(self) -> None:
        self.translate_and_replace_clicked.emit()

    def _on_open_settings_clicked(self) -> None:
        self.hide()
        self.open_language_settings_requested.emit()

    def show_near(self, anchor_widget: QWidget) -> None:
        geo = anchor_widget.geometry()
        x = max(12, geo.left() - self.width() - 10)
        y = max(12, geo.bottom() - self.sizeHint().height())
        self.move(x, y)
        self.show()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._apply_win32_no_activate()

    def nativeEvent(self, event_type: Any, message: Any):
        res = self._handle_native_mouse_activate(message)
        if res is not None:
            return res
        return super().nativeEvent(event_type, message)


class FloatingPopupCard(QWidget, _NoActivateWindowMixin):
    """Card auxiliar para leitura de mensagens quando solicitado na janela principal."""

    apply_replacement_requested = Signal(str)
    copy_requested = Signal(str)

    def __init__(self, inspector: Win32WindowInspector | None = None) -> None:
        super().__init__(None)
        self._inspector = inspector or Win32WindowInspector()
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(360, 210)
        self.setStyleSheet(
            """
            QWidget {
                background-color: #101828;
                color: #F2F4F7;
                border: 1px solid #2563EB;
                border-radius: 10px;
                font-family: 'Inter', 'Segoe UI', Arial;
            }
            QLabel { border: none; }
            QPlainTextEdit {
                background-color: #1D2939;
                border: 1px solid #344054;
                border-radius: 6px;
                color: #FFFFFF;
                padding: 6px;
            }
            QPushButton {
                background-color: #2563EB;
                color: white;
                border: none;
                padding: 6px 12px;
                border-radius: 5px;
                font-weight: 600;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.title_label = QLabel("Tradução")
        self.title_label.setStyleSheet("font-weight: 700; color: #FFFFFF;")
        header.addWidget(self.title_label)
        header.addStretch()

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.clicked.connect(self.hide)
        header.addWidget(close_btn)
        layout.addLayout(header)

        self.text_area = QPlainTextEdit()
        layout.addWidget(self.text_area)

        actions = QHBoxLayout()
        self.copy_btn = QPushButton("Copiar")
        self.copy_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.copy_btn.clicked.connect(
            lambda: self.copy_requested.emit(self.text_area.toPlainText())
        )
        actions.addWidget(self.copy_btn)

        self.replace_btn = QPushButton("Substituir no Campo")
        self.replace_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.replace_btn.setStyleSheet("background-color: #16A36A;")
        self.replace_btn.clicked.connect(self._on_replace_clicked)
        actions.addWidget(self.replace_btn)
        layout.addLayout(actions)

    def _on_replace_clicked(self) -> None:
        text = self.text_area.toPlainText().strip()
        if text:
            self.apply_replacement_requested.emit(text)
            self.hide()

    def show_translation(
        self,
        header_text: str,
        translated_text: str,
        anchor_pos: QPoint | None = None,
        allow_replace: bool = True,
    ) -> None:
        self.title_label.setText(header_text)
        self.text_area.setPlainText(translated_text)
        self.replace_btn.setVisible(allow_replace)
        if anchor_pos is not None:
            self.move(max(20, anchor_pos.x() - 370), max(20, anchor_pos.y() - 180))
        self.show()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._apply_win32_no_activate()

    def nativeEvent(self, event_type: Any, message: Any):
        res = self._handle_native_mouse_activate(message)
        if res is not None:
            return res
        return super().nativeEvent(event_type, message)


class FloatingButton(QWidget, _NoActivateWindowMixin):
    """Botão flutuante discreto e moderno com três áreas interativas independentes (Seção 2):
    - Centro: ativação e desativação imediata da tradução automática.
    - Canto superior esquerdo (`language_chip`): configuração rápida de idiomas e detecção automática.
    - Canto superior direito (`close_chip`): ícone X para fechar o botão flutuante e interromper monitoramento.
    """

    panel_opened_for_hwnd = Signal(int)
    translate_outgoing_requested = Signal()
    translate_incoming_requested = Signal()
    rollback_requested = Signal()
    cancel_requested = Signal()
    closed_by_user = Signal()
    automatic_mode_changed = Signal(bool)
    auto_detect_changed = Signal(bool)
    open_language_settings_requested = Signal()
    position_changed = Signal(int, int)
    source_language_changed = Signal(str, str)
    target_language_changed = Signal(str, str)

    def __init__(
        self,
        parent_window: Any = None,
        inspector: Win32WindowInspector | None = None,
    ) -> None:
        super().__init__(None)
        self.parent_app = parent_window
        self._inspector = inspector or Win32WindowInspector()
        self._state = FloatingButtonState.IDLE
        self._drag_start_pos: QPoint | None = None
        self._widget_start_pos: QPoint | None = None
        self._was_dragged = False
        self.last_external_hwnd: int | None = None
        self.automatic_mode = False

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(62, 62)

        # Área Central: Controle principal de ativação/desativação do modo automático
        self.button = QPushButton("文", self)
        self.button.setGeometry(4, 4, 54, 54)
        self.button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.button.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        # Canto Superior Esquerdo: Ícone de configuração rápida de idiomas
        self.language_chip = QPushButton("文", self)
        self.language_chip.setGeometry(0, 0, 20, 20)
        self.language_chip.setToolTip("Configuração rápida de idiomas")
        self.language_chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.language_chip.clicked.connect(self.toggle_action_panel)

        # Canto Superior Direito: Ícone X para fechar o botão flutuante e parar o monitoramento
        self.close_chip = QPushButton("×", self)
        self.close_chip.setGeometry(42, 0, 20, 20)
        self.close_chip.setToolTip("Fechar botão flutuante e interromper monitoramento")
        self.close_chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_chip.clicked.connect(self.close_floating_button)
        self._style_chips()

        self.action_panel = SimpleFloatingActionPanel(inspector=self._inspector)
        self.action_panel.source_language_selected.connect(self.source_language_changed.emit)
        self.action_panel.target_language_selected.connect(self.target_language_changed.emit)
        self.action_panel.auto_detect_toggled.connect(self.auto_detect_changed.emit)
        self.action_panel.open_language_settings_requested.connect(
            self.open_language_settings_requested.emit
        )
        self.action_panel.translate_and_replace_clicked.connect(self._on_panel_translate_clicked)

        self.set_visual_state(
            FloatingButtonState.IDLE,
            "Centro: ativar/desativar tradução automática | Canto sup. esq.: idiomas | ×: fechar",
        )
        self._place_initial_position()

    def _style_chips(self) -> None:
        lang_style = (
            "QPushButton{background:#101828;color:#F2F4F7;border:1px solid #98A2B3;"
            "border-radius:10px;font-size:10px;font-weight:700;padding:0;} "
            "QPushButton:hover{background:#2563EB;border-color:#FFFFFF;}"
        )
        close_style = (
            "QPushButton{background:#D92D20;color:#FFFFFF;border:1px solid #FFFFFF;"
            "border-radius:10px;font-size:12px;font-weight:700;padding:0;} "
            "QPushButton:hover{background:#B42318;}"
        )
        self.language_chip.setStyleSheet(lang_style)
        self.close_chip.setStyleSheet(close_style)

    def set_automatic_mode(self, enabled: bool, emit_signal: bool = True) -> None:
        new_val = bool(enabled)
        changed = self.automatic_mode != new_val
        self.automatic_mode = new_val
        self.set_visual_state(self._state)
        if changed and emit_signal:
            self.automatic_mode_changed.emit(self.automatic_mode)

    def toggle_automatic_mode(self) -> None:
        self.set_automatic_mode(not self.automatic_mode, emit_signal=True)

    def close_floating_button(self) -> None:
        """Fecha o painel, desativa imediatamente o modo automático e oculta o botão flutuante."""
        if self.automatic_mode:
            self.set_automatic_mode(False, emit_signal=True)
        self.action_panel.hide()
        self.hide()
        self.closed_by_user.emit()

    def _record_external_foreground_hwnd(self) -> int | None:
        fg = self._inspector.get_foreground_hwnd()
        if fg:
            ctx = self._inspector.get_window_context_for_hwnd(fg)
            if ctx and not self._inspector.is_own_process(ctx):
                self.last_external_hwnd = int(fg)
        return self.last_external_hwnd

    def _on_panel_translate_clicked(self) -> None:
        self._record_external_foreground_hwnd()
        if self.parent_app and hasattr(self.parent_app, "translate_selection"):
            self.parent_app.translate_selection()
        else:
            self.translate_outgoing_requested.emit()

    def set_source_language(self, code_or_name: str) -> None:
        self.action_panel.set_source_language_by_code_or_name(code_or_name)

    def set_target_language(self, code_or_name: str) -> None:
        self.action_panel.set_target_language_by_code_or_name(code_or_name)

    def set_auto_detect(self, enabled: bool) -> None:
        self.action_panel.set_auto_detect(enabled)

    def _place_initial_position(self, x: int = -1, y: int = -1) -> None:
        if x >= 0 and y >= 0:
            self.move(x, y)
            return
        screen = QGuiApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.move(area.right() - 90, area.bottom() - 120)

    def set_saved_position(self, x: int, y: int) -> None:
        if x >= 0 and y >= 0:
            self.move(x, y)

    def set_visual_state(self, state: FloatingButtonState, tooltip: str = "") -> None:
        self._state = state
        styles = {
            FloatingButtonState.IDLE: ("#344054", "#475467", "文"),
            FloatingButtonState.CAPTURED: ("#16A36A", "#0D8051", "文"),
            FloatingButtonState.TRANSLATING: ("#F79009", "#B54708", "⏳"),
            FloatingButtonState.SUCCESS: ("#16A36A", "#0D8051", "✓"),
            FloatingButtonState.WARNING: ("#F79009", "#B54708", "!"),
            FloatingButtonState.ERROR: ("#D92D20", "#B42318", "✕"),
        }
        bg, border, label = styles.get(state, ("#344054", "#475467", "文"))
        if self.automatic_mode and state not in (
            FloatingButtonState.TRANSLATING,
            FloatingButtonState.ERROR,
        ):
            bg, border, label = "#2563EB", "#60A5FA", "✓"
        self.button.setText(label)
        if tooltip:
            self.setToolTip(tooltip)
            self.button.setToolTip(tooltip)
        self.button.setStyleSheet(
            f"""
            QPushButton {{
                background: {bg};
                color: white;
                border: 2px solid {border};
                border-radius: 27px;
                font-size: 20px;
                font-weight: bold;
            }}
            """
        )
        if state == FloatingButtonState.TRANSLATING:
            self.action_panel.translate_replace_button.setEnabled(False)
            self.action_panel.translate_replace_button.setText("Traduzindo e substituindo...")
        else:
            self.action_panel.translate_replace_button.setEnabled(True)
            self.action_panel.translate_replace_button.setText("Traduzir e Substituir Agora")

    def toggle_action_panel(self) -> None:
        if self.action_panel.isVisible():
            self.action_panel.hide()
        else:
            ext_hwnd = self._record_external_foreground_hwnd()
            self.action_panel.show_near(self)
            if ext_hwnd:
                self.panel_opened_for_hwnd.emit(int(ext_hwnd))

    def _show_right_click_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        menu.setWindowFlags(
            menu.windowFlags()
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        menu.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        menu.setStyleSheet(
            """
            QMenu {
                background-color: #1D2939;
                color: #F2F4F7;
                border: 1px solid #344054;
                padding: 4px;
            }
            QMenu::item {
                padding: 7px 14px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #2563EB;
                color: #FFFFFF;
            }
            """
        )
        toggle_act = QAction(
            "Desativar Tradução Automática" if self.automatic_mode else "Ativar Tradução Automática",
            self,
        )
        toggle_act.triggered.connect(self.toggle_automatic_mode)
        menu.addAction(toggle_act)

        lang_act = QAction("Configurar Idiomas", self)
        lang_act.triggered.connect(self.toggle_action_panel)
        menu.addAction(lang_act)

        close_act = QAction("✕ Fechar Botão Flutuante", self)
        close_act.triggered.connect(self.close_floating_button)
        menu.addAction(close_act)
        menu.exec(global_pos)

    def enterEvent(self, event: Any) -> None:
        self._record_external_foreground_hwnd()
        super().enterEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        self._record_external_foreground_hwnd()
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.globalPosition().toPoint()
            self._widget_start_pos = self.pos()
            self._was_dragged = False
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self._show_right_click_menu(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._drag_start_pos is not None and self._widget_start_pos is not None:
            delta = event.globalPosition().toPoint() - self._drag_start_pos
            if delta.manhattanLength() > 6:
                self._was_dragged = True
                new_pos = self._widget_start_pos + delta
                self.move(new_pos)
                if self.action_panel.isVisible():
                    self.action_panel.show_near(self)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            was_dragged = self._was_dragged
            self._drag_start_pos = None
            self._widget_start_pos = None
            self._was_dragged = False

            if was_dragged:
                self.position_changed.emit(self.x(), self.y())
            else:
                self.toggle_automatic_mode()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def hideEvent(self, event: Any) -> None:
        self.action_panel.hide()
        super().hideEvent(event)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self._apply_win32_no_activate()

    def nativeEvent(self, event_type: Any, message: Any):
        res = self._handle_native_mouse_activate(message)
        if res is not None:
            return res
        return super().nativeEvent(event_type, message)
