"""Testes Unitários: Glossário, Detector de Idioma, Perfis por Aplicativo, Cofre de Credenciais e Sanitização de Logs."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from universal_translator.config.settings_manager import AppSettings, SettingsManager
from universal_translator.domain.entities import (
    ApplicationProfile,
    GlossaryEntry,
    WindowContext,
)
from universal_translator.domain.enums import LogCategory, ProviderType
from universal_translator.domain.services import (
    GlossaryEngine,
    LanguageDetector,
    ProfileMatcher,
)
from universal_translator.infrastructure.logging.logger import (
    ObservabilityLogger,
    PrivacyRedactingFilter,
)
from universal_translator.infrastructure.security.credential_manager import (
    CredentialManager,
)


class TestDomainAndSecurity(unittest.TestCase):
    def test_glossary_engine_pre_and_post_processing(self) -> None:
        entries = [
            GlossaryEntry(
                source_term="áreas contaminadas",
                target_term="contaminated sites",
                source_lang="PT",
                target_lang="EN",
                case_sensitive=False,
                do_not_translate=False,
                priority=20,
            ),
            GlossaryEntry(
                source_term="EQuIS",
                target_term="EQuIS",
                source_lang="ANY",
                target_lang="ANY",
                case_sensitive=True,
                do_not_translate=True,
                priority=50,
            ),
        ]
        engine = GlossaryEngine(entries)
        original = "O relatório de Áreas Contaminadas foi exportado do EQuIS hoje."
        pre = engine.pre_process(original, source_lang="PT-BR", target_lang="EN-US")

        self.assertNotIn("Áreas Contaminadas", pre.prepared_text)
        self.assertNotIn("EQuIS", pre.prepared_text)
        self.assertEqual(len(pre.token_map), 2)

        # Simula provedor traduzindo a frase ao redor dos marcadores
        simulated_provider = pre.prepared_text.replace(
            "O relatório de", "The report on"
        ).replace("foi exportado do", "was exported from").replace("hoje.", "today.")

        final_text = engine.post_process(simulated_provider, pre.token_map)
        self.assertIn("contaminated sites", final_text)
        self.assertIn("EQuIS", final_text)
        self.assertEqual(
            final_text,
            "The report on contaminated sites was exported from EQuIS today.",
        )

    def test_language_detector_identifies_pt_en_es_ja(self) -> None:
        self.assertEqual(
            LanguageDetector.detect("Você poderia me enviar o relatório amanhã?"),
            "PT-BR",
        )
        self.assertEqual(
            LanguageDetector.detect("Could you please send me the report tomorrow?"),
            "EN-US",
        )
        self.assertEqual(
            LanguageDetector.detect("¿Podría enviarme el informe mañana por favor?"),
            "ES",
        )
        self.assertEqual(
            LanguageDetector.detect("明日レポートを送っていただけますか？"),
            "JA",
        )

    def test_profile_matcher_matches_title_and_process(self) -> None:
        matcher = ProfileMatcher()
        ctx_whatsapp = WindowContext(
            hwnd=101,
            process_name="chrome.exe",
            window_title="(2) WhatsApp - Google Chrome",
        )
        prof_wa = matcher.match(ctx_whatsapp)
        self.assertIn("WhatsApp", prof_wa.app_name)

        ctx_slack = WindowContext(
            hwnd=202,
            process_name="slack.exe",
            window_title="Canal #engenharia",
        )
        prof_slack = matcher.match(ctx_slack)
        self.assertEqual(prof_slack.app_name, "Slack")

        ctx_unknown = WindowContext(
            hwnd=303,
            process_name="custom_app.exe",
            window_title="Unknown Window",
        )
        prof_default = matcher.match(ctx_unknown)
        self.assertEqual(prof_default.app_name, "Padrão Global")

    def test_settings_never_store_plaintext_api_keys_and_migrate_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            vault_file = tmp_path / "test.vault"
            settings_file = tmp_path / "settings.json"

            # Simula arquivo settings.json legado com api_key em texto puro
            legacy_payload = {
                "language": "English (EUA)",
                "api_key": "12345678-aaaa-bbbb-cccc-1234567890ab:fx",
                "show_floating_button": True,
            }
            settings_file.write_text(json.dumps(legacy_payload), encoding="utf-8")

            cred = CredentialManager(vault_path=vault_file)
            cred._win_cred_available = False  # Força teste determinístico do Vault Criptografado
            cred._keyring_mod = None

            mgr = SettingsManager(settings_path=settings_file, credential_manager=cred)
            loaded = mgr.load()
            self.assertEqual(loaded.language, "English (EUA)")

            # Verifica que a chave foi migrada para o cofre seguro
            retrieved_key = mgr.get_provider_api_key(ProviderType.DEEPL)
            self.assertEqual(retrieved_key, "12345678-aaaa-bbbb-cccc-1234567890ab:fx")

            # Verifica que o arquivo settings.json em disco NÃO contém mais api_key
            raw_on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertNotIn("api_key", raw_on_disk)
            self.assertNotIn("12345678-aaaa", settings_file.read_text(encoding="utf-8"))

            # Verifica que o arquivo .vault também está criptografado e não expõe a chave em texto puro
            vault_raw_text = vault_file.read_text(encoding="utf-8")
            self.assertNotIn("12345678-aaaa", vault_raw_text)

    def test_logger_redacts_api_keys_and_sensitive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = ObservabilityLogger(log_dir=Path(tmpdir))
            raw_msg = (
                "Falha com Header Authorization: DeepL-Auth-Key 99887766-1111-2222-3333-444455556666:fx "
                "e OpenAI sk-proj1234567890abcdefghijklmno"
            )
            logger.error(
                LogCategory.SECURITY,
                raw_msg,
                operation_id="sec_test",
                details={"api_key": "secret_val", "source_text": "Mensagem privada do usuário"},
            )
            records = logger.get_recent_records()
            self.assertEqual(len(records), 1)
            rec = records[0]
            self.assertNotIn("99887766", rec.message)
            self.assertNotIn("sk-proj123456", rec.message)
            self.assertIn("[REDACTED", rec.message)
            self.assertTrue(str(rec.details.get("source_text", "")).startswith("[REDACTED_LEN_"))


if __name__ == "__main__":
    unittest.main()
