"""Testes de Regressão, Automação de 5 Segundos, Proteção contra Interferências, UI e Empacotamento Windows."""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from universal_translator.application.orchestrator import TranslationOrchestrator
from universal_translator.config.settings_manager import SettingsManager
from universal_translator.domain.entities import (
    TranslationResult,
    WindowContext,
)
from universal_translator.domain.enums import (
    CaptureMethod,
    ControlAccessibilityKind,
    ProviderType,
    ReplacementSafetyStatus,
)
from universal_translator.infrastructure.accessibility.ui_automation_adapter import (
    UIAutomationAdapter,
)
from universal_translator.infrastructure.clipboard.safe_clipboard_engine import (
    SafeTextCaptureEngine,
)
from universal_translator.infrastructure.persistence.sqlite_repository import SQLiteRepository
from universal_translator.infrastructure.persistence.usage_tracker import UsageTracker
from universal_translator.infrastructure.providers.deepl_provider import DeepLProvider
from universal_translator.infrastructure.providers.resilience import TranslationProviderError
from universal_translator.infrastructure.security.credential_manager import CredentialManager
from universal_translator.infrastructure.windows.startup_manager import WindowsStartupManager
from universal_translator.infrastructure.windows.win32_inspector import Win32WindowInspector


class TestCaptureReplaceAndRegressions(unittest.TestCase):
    def _create_mocked_engine(
        self,
        initial_clipboard: str = "MEU_CONTEUDO_IMPORTANTE_ANTERIOR",
        selected_text_on_copy: str = "Você poderia me enviar o relatório amanhã?",
        fg_hwnd: int = 1001,
        editable: bool = True,
        is_password: bool = False,
        uia_full_text: str = "",
        control_id: str = "hwnd:2001",
    ) -> tuple[SafeTextCaptureEngine, dict[str, str], list[tuple[str, str]], MagicMock, MagicMock]:
        clipboard_state = {"text": initial_clipboard, "seq": 10}
        sent_hotkeys: list[tuple[str, str]] = []
        field_state = {"text": uia_full_text or selected_text_on_copy, "ctrl_id": control_id}

        inspector = MagicMock(spec=Win32WindowInspector)
        inspector.get_foreground_hwnd.return_value = fg_hwnd
        inspector.is_window_valid.return_value = True
        inspector.is_own_process.return_value = False
        inspector.activate_window_safely.return_value = True
        inspector.is_sensitive_window_context.return_value = is_password
        inspector.get_clipboard_sequence_number.side_effect = lambda: clipboard_state["seq"]
        inspector.get_window_context_for_hwnd.side_effect = lambda h: WindowContext(
            hwnd=h,
            process_id=4321,
            process_name="chrome.exe",
            window_title="WhatsApp Web - Google Chrome",
            class_name="Chrome_WidgetWin_1",
            focused_control_hwnd=2001,
            focused_control_id=field_state["ctrl_id"],
            is_password_or_sensitive=is_password,
        )

        uia = MagicMock(spec=UIAutomationAdapter)
        uia.try_capture_selection.return_value = ("", None)
        uia.try_replace_selection.return_value = (False, None)
        uia.is_focused_editable_control.return_value = editable and not is_password
        uia.is_password_or_sensitive_control.return_value = is_password
        uia.classify_focused_control.return_value = (
            ControlAccessibilityKind.SENSITIVE_OR_PASSWORD
            if is_password
            else (
                ControlAccessibilityKind.EDITABLE_FIELD
                if editable
                else ControlAccessibilityKind.SELECTABLE_TEXT
            )
        )
        uia.get_focused_control_identifier.side_effect = lambda h: (2001, field_state["ctrl_id"])
        uia.get_cursor_selection_range.return_value = (0, 0)

        if uia_full_text:
            uia.try_capture_entire_field.side_effect = lambda h: (
                field_state["text"],
                CaptureMethod.UI_AUTOMATION,
                (len(field_state["text"]), len(field_state["text"])),
            )

            def _uia_replace(h: int, new_val: str, preserve_cursor: bool = True):
                field_state["text"] = new_val
                return True, CaptureMethod.UI_AUTOMATION

            uia.try_replace_entire_field.side_effect = _uia_replace
        else:
            uia.try_capture_entire_field.return_value = ("", None, None)
            uia.try_replace_entire_field.return_value = (False, None)

        def _read_clip() -> str:
            return clipboard_state["text"]

        def _write_clip(val: str) -> None:
            clipboard_state["text"] = val
            clipboard_state["seq"] += 1

        def _send_hotkey(mod: str, key: str) -> None:
            sent_hotkeys.append((mod, key))
            if (mod, key) == ("ctrl", "c"):
                clipboard_state["text"] = field_state["text"]
                clipboard_state["seq"] += 1
            elif (mod, key) == ("ctrl", "v"):
                field_state["text"] = clipboard_state["text"]

        engine = SafeTextCaptureEngine(
            window_inspector=inspector,
            uia_adapter=uia,
            clipboard_read_fn=_read_clip,
            clipboard_write_fn=_write_clip,
            hotkey_send_fn=_send_hotkey,
        )
        engine._field_state = field_state  # type: ignore[attr-defined]
        return engine, clipboard_state, sent_hotkeys, inspector, uia

    def test_capture_preserves_original_clipboard_and_never_leaves_marker(self) -> None:
        engine, clip_state, hotkeys, _, _ = self._create_mocked_engine(
            initial_clipboard="DADOS_DO_USUARIO_NA_CLIPBOARD",
            selected_text_on_copy="Você poderia me enviar o relatório amanhã?",
        )
        res = engine.capture_selection(target_hwnd=1001)
        self.assertTrue(res.success)
        self.assertEqual(res.text, "Você poderia me enviar o relatório amanhã?")
        self.assertEqual(res.method, CaptureMethod.CONTROLLED_CLIPBOARD)
        self.assertEqual(clip_state["text"], "DADOS_DO_USUARIO_NA_CLIPBOARD")
        self.assertIn(("ctrl", "c"), hotkeys)

    def test_capture_copied_clipboard_reads_ctrl_c_directly_without_redundant_hotkey(self) -> None:
        engine, _, hotkeys, _, _ = self._create_mocked_engine(
            initial_clipboard="Você poderia me enviar o relatório amanhã?",
        )
        res = engine.capture_copied_clipboard(target_hwnd=1001)
        self.assertTrue(res.success)
        self.assertEqual(res.text, "Você poderia me enviar o relatório amanhã?")
        self.assertEqual(len(hotkeys), 0)

    def test_replace_aborts_if_target_window_closed_or_focus_stolen(self) -> None:
        engine, _, hotkeys, inspector, _ = self._create_mocked_engine()
        ctx = WindowContext(hwnd=1001, process_name="notepad.exe", window_title="Bloco de Notas")

        inspector.is_window_valid.return_value = False
        rep_closed = engine.replace_text_safely("Could you send me the report tomorrow?", ctx)
        self.assertFalse(rep_closed.success)
        self.assertEqual(rep_closed.safety_status, ReplacementSafetyStatus.WINDOW_INVALID)
        self.assertNotIn(("ctrl", "v"), hotkeys)

        inspector.is_window_valid.return_value = True
        inspector.get_foreground_hwnd.return_value = 9999
        rep_focus = engine.replace_text_safely("Could you send me the report tomorrow?", ctx)
        self.assertFalse(rep_focus.success)
        self.assertEqual(rep_focus.safety_status, ReplacementSafetyStatus.FOCUS_CHANGED)
        self.assertNotIn(("ctrl", "v"), hotkeys)

    def test_replace_and_rollback_restores_original_text(self) -> None:
        engine, clip_state, hotkeys, _, _ = self._create_mocked_engine()
        ctx = WindowContext(hwnd=1001, process_name="notepad.exe", window_title="Bloco de Notas")

        rep = engine.replace_text_safely(
            new_text="Could you send me the report tomorrow?",
            original_context=ctx,
            expected_original_text="Você poderia me enviar o relatório amanhã?",
            verify_content_before_paste=False,
        )
        self.assertTrue(rep.success)
        self.assertTrue(rep.rollback_available)
        self.assertIn(("ctrl", "v"), hotkeys)
        self.assertEqual(clip_state["text"], "MEU_CONTEUDO_IMPORTANTE_ANTERIOR")

        undo_res = engine.rollback_last_replacement()
        self.assertTrue(undo_res.success)
        self.assertEqual(undo_res.new_text, "Você poderia me enviar o relatório amanhã?")

    def test_replace_never_sends_enter_key_automatically(self) -> None:
        engine, _, hotkeys, _, _ = self._create_mocked_engine()
        ctx = WindowContext(hwnd=1001, process_name="whatsapp.exe", window_title="WhatsApp")
        engine.replace_text_safely("Hello!", ctx)
        for _, key in hotkeys:
            self.assertNotIn("enter", key.lower())
            self.assertNotIn("return", key.lower())

    def test_full_field_capture_supports_long_multipara_emojis_and_special_chars(self) -> None:
        complex_message = (
            "Olá, tudo bem? 😊\n\n"
            "Gostaria de saber se você recebeu o documento técnico (Versão #2.4 — São Paulo/SP) "
            "que enviei ontem às 18h30.\n"
            "Parágrafo 3 com símbolos: R$ 1.250,00, 100% concluído & validado!\n\n"
            + ("Linha extensa de auditoria ambiental com dados completos. " * 25)
        )
        engine, clip_state, _, _, _ = self._create_mocked_engine(
            initial_clipboard="CLIPBOARD_INTACTO",
            uia_full_text=complex_message,
        )
        cap = engine.capture_entire_field(target_hwnd=1001)
        self.assertTrue(cap.success)
        self.assertEqual(cap.method, CaptureMethod.UI_AUTOMATION)
        self.assertEqual(cap.text, complex_message)
        self.assertEqual(clip_state["text"], "CLIPBOARD_INTACTO")

    def test_password_and_sensitive_fields_are_strictly_blocked(self) -> None:
        engine, _, hotkeys, _, _ = self._create_mocked_engine(
            selected_text_on_copy="MinhaSenhaSecreta123!",
            is_password=True,
        )
        cap = engine.capture_entire_field(target_hwnd=1001)
        self.assertFalse(cap.success)
        self.assertIn("senha", cap.error_message.lower())
        self.assertEqual(len(hotkeys), 0)

    def test_auto_monitor_5s_timer_reset_anti_loop_window_and_field_change_and_api_error_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = SQLiteRepository(db_path=tmp / "auto.db")
            cred = CredentialManager(vault_path=tmp / "auto.vault")
            cred._win_cred_available = False
            cred._keyring_mod = None
            sm = SettingsManager(settings_path=tmp / "settings.json", credential_manager=cred)
            tracker = UsageTracker(tmp / "usage.db")

            whatsapp_msg = "Olá, tudo bem? Gostaria de saber se você recebeu o documento que enviei ontem."
            translated_msg = "Hello, how are you? I would like to know if you received the document I sent yesterday."

            engine, _, _, inspector, _ = self._create_mocked_engine(
                uia_full_text=whatsapp_msg,
                fg_hwnd=1001,
                control_id="uia:whatsapp_input_box",
            )

            mock_deepl = MagicMock(spec=DeepLProvider)
            mock_deepl.provider_type = ProviderType.DEEPL
            mock_deepl.is_configured.return_value = True
            mock_deepl.translate.side_effect = lambda req: TranslationResult(
                translated_text=translated_msg,
                source_text=req.text,
                detected_source_lang="PT",
                target_lang=req.target_lang,
                provider=ProviderType.DEEPL,
                characters_billed=len(req.text),
                metadata={"billed_characters_confirmed": True},
            )

            orch = TranslationOrchestrator(
                settings_manager=sm,
                repository=repo,
                capture_engine=engine,
                usage_tracker=tracker,
                providers={ProviderType.DEEPL: mock_deepl},
            )

            timer_restarts: list[int] = []
            timer_stops: list[bool] = []
            mon = orch.auto_monitor
            mon._on_timer_restart = timer_restarts.append
            mon._on_timer_stop = lambda: timer_stops.append(True)
            mon.set_enabled(True)

            # 1. Digitação, Backspace e colagem reiniciam o temporizador de 5000ms
            self.assertTrue(mon.on_key_event("o", 1001, "uia:whatsapp_input_box"))
            s1 = mon.activity_serial
            self.assertTrue(mon.on_key_event("backspace", 1001, "uia:whatsapp_input_box"))
            s2 = mon.activity_serial
            self.assertTrue(mon.on_paste_or_content_event(1001, "uia:whatsapp_input_box"))
            s3 = mon.activity_serial
            self.assertGreater(s2, s1)
            self.assertGreater(s3, s2)
            self.assertEqual(timer_restarts[-1], 5000)

            # 2. Ausência de movimento ou movimento puro do mouse NÃO dispara nem reinicia
            self.assertFalse(mon.on_mouse_interaction("MoveEvent", 1001, "uia:whatsapp_input_box"))

            # 3. Após 5 segundos de inatividade, executa o ciclo e substitui o campo pela tradução
            outcome1 = mon.execute_idle_cycle(source_lang="AUTO", target_lang="EN-US")
            self.assertTrue(outcome1.executed)
            self.assertEqual(engine._field_state["text"], translated_msg)  # type: ignore[attr-defined]

            # 4. Proteção contra interferências: NÃO traduz o próprio resultado nem repete a mesma mensagem
            mon.on_key_event("right", 1001, "uia:whatsapp_input_box")
            mon._has_pending_edit_content = True
            outcome_repeat = mon.execute_idle_cycle(source_lang="AUTO", target_lang="EN-US")
            self.assertFalse(outcome_repeat.executed)
            self.assertIn("próprio resultado", outcome_repeat.skipped_reason)

            # 5. Troca de campo ou troca de janela cancela a captura/substituição
            engine._field_state["text"] = "Nova mensagem em edição"  # type: ignore[attr-defined]
            mon.on_key_event("a", 1001, "uia:whatsapp_input_box")
            self.assertTrue(mon.on_window_or_field_changed(1001, "uia:search_contact_box"))
            self.assertFalse(mon.has_pending_edit)

            mon.on_key_event("b", 1001, "uia:whatsapp_input_box")
            inspector.get_foreground_hwnd.return_value = 9999  # Usuário trocou de janela
            outcome_win_change = mon.execute_idle_cycle(source_lang="AUTO", target_lang="EN-US")
            self.assertFalse(outcome_win_change.executed)
            self.assertEqual(engine._field_state["text"], "Nova mensagem em edição")  # type: ignore[attr-defined]

            # 6. Falha da API DeepL NUNCA apaga ou substitui o texto original
            inspector.get_foreground_hwnd.return_value = 1001
            engine._field_state["ctrl_id"] = "uia:whatsapp_input_box"  # type: ignore[attr-defined]
            engine._field_state["text"] = "Texto importante que não pode ser perdido."  # type: ignore[attr-defined]
            mon.on_key_event("x", 1001, "uia:whatsapp_input_box")
            mock_deepl.translate.side_effect = TranslationProviderError(
                "HTTP 456 Quota Exceeded",
                provider=ProviderType.DEEPL,
                status_code=456,
                quota_exceeded=True,
            )
            with self.assertRaises(TranslationProviderError):
                mon.execute_idle_cycle(source_lang="AUTO", target_lang="EN-US")
            self.assertEqual(
                engine._field_state["text"],  # type: ignore[attr-defined]
                "Texto importante que não pode ser perdido.",
            )

            # 7. Fechamento do botão flutuante (close_and_stop) interrompe o monitoramento imediatamente
            mon.close_and_stop()
            self.assertFalse(mon.enabled)
            self.assertFalse(mon.on_key_event("z", 1001, "uia:whatsapp_input_box"))

    def test_ui_floating_button_three_areas_and_main_window_six_sections_and_packaging(self) -> None:
        # Verifica gerenciador de inicialização com Windows e arquivo PyInstaller .spec
        startup = WindowsStartupManager(is_win32=False)
        self.assertTrue(startup.set_enabled(True))
        self.assertTrue(startup.is_enabled())
        self.assertTrue(startup.set_enabled(False))
        self.assertFalse(startup.is_enabled())

        project_root = Path(__file__).resolve().parents[2]
        spec_path = project_root / "UniversalTranslatorPro.spec"
        self.assertTrue(spec_path.exists())
        spec_text = spec_path.read_text(encoding="utf-8")
        self.assertIn("UniversalTranslatorPro", spec_text)
        self.assertIn("universal_translator.application.auto_monitor", spec_text)
        self.assertIn("universal_translator.infrastructure.windows.startup_manager", spec_text)

        # Valida sintaxe AST e estrutura das 6 seções de MainWindow e 3 áreas de FloatingButton
        mw_path = project_root / "src" / "universal_translator" / "presentation" / "windows" / "main_window.py"
        fb_path = project_root / "src" / "universal_translator" / "presentation" / "widgets" / "floating_translator.py"
        mw_source = mw_path.read_text(encoding="utf-8")
        fb_source = fb_path.read_text(encoding="utf-8")
        ast.parse(mw_source)
        ast.parse(fb_source)

        for section_title in (
            "1. Visão Geral",
            "2. Idiomas",
            "3. DeepL",
            "4. Traduções",
            "5. Consumo",
            "6. Automação",
        ):
            self.assertIn(section_title, mw_source)

        for fb_control in (
            "self.button",
            "self.language_chip",
            "self.close_chip",
            "self.auto_detect_checkbox",
            "self.open_language_settings_button",
            "close_floating_button",
            "toggle_automatic_mode",
        ):
            self.assertIn(fb_control, fb_source)


if __name__ == "__main__":
    unittest.main()
