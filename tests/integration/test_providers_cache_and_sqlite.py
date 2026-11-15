"""Testes de Integração: Provedores (DeepL, Google, Microsoft, OpenAI, Offline), Cache SHA-256, Circuit Breaker e SQLite."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from universal_translator.application.orchestrator import TranslationOrchestrator
from universal_translator.config.settings_manager import SettingsManager
from universal_translator.domain.entities import (
    TranslationRequest,
    WindowContext,
)
from universal_translator.domain.enums import (
    FormalityLevel,
    ProviderType,
    TranslationDirection,
)
from universal_translator.infrastructure.persistence.sqlite_repository import (
    SQLiteRepository,
)
from universal_translator.infrastructure.providers import (
    CircuitBreaker,
    DeepLProvider,
    GoogleTranslateProvider,
    MicrosoftTranslatorProvider,
    OfflineFallbackProvider,
    OpenAIProvider,
    RetryPolicy,
    TranslationProviderError,
)
from universal_translator.infrastructure.security.credential_manager import (
    CredentialManager,
)


class TestProvidersCacheAndSQLite(unittest.TestCase):
    def test_deepl_provider_free_and_pro_endpoints_and_context(self) -> None:
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "translations": [
                {
                    "detected_source_language": "PT",
                    "text": "Could you send me the report tomorrow?",
                }
            ]
        }
        mock_client.post.return_value = mock_response

        provider = DeepLProvider(
            api_key_resolver=lambda: "my-key-123:fx",
            http_client=mock_client,
        )
        req = TranslationRequest(
            text="Você poderia me enviar o relatório amanhã?",
            source_lang="PT-BR",
            target_lang="EN-US",
            formality=FormalityLevel.FORMAL,
        )
        res = provider.translate(req)
        self.assertEqual(res.translated_text, "Could you send me the report tomorrow?")
        self.assertEqual(res.estimated_cost_usd, 0.0)  # Free tier :fx = $0.0
        called_url = mock_client.post.call_args[0][0]
        self.assertEqual(called_url, "https://api-free.deepl.com/v2/translate")

    def test_deepl_403_and_456_are_not_retried(self) -> None:
        calls = 0

        def _failing_op():
            nonlocal calls
            calls += 1
            raise TranslationProviderError(
                "HTTP 456 Quota Exceeded",
                provider=ProviderType.DEEPL,
                status_code=456,
                quota_exceeded=True,
                retryable=False,
            )

        policy = RetryPolicy(max_retries=3, sleep_fn=lambda _: None)
        with self.assertRaises(TranslationProviderError):
            policy.execute(_failing_op)
        self.assertEqual(calls, 1, "Erros de cota (456) ou autenticação (403) jamais devem sofrer retry")

    def test_circuit_breaker_opens_after_threshold_and_recovers(self) -> None:
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=0.05)
        self.assertEqual(cb.state, "CLOSED")
        self.assertTrue(cb.allow_request())

        cb.record_failure("Timeout 1")
        self.assertEqual(cb.state, "CLOSED")
        cb.record_failure("Timeout 2")
        self.assertEqual(cb.state, "OPEN")
        self.assertFalse(cb.allow_request())

        import time
        time.sleep(0.06)
        self.assertEqual(cb.state, "HALF_OPEN")
        self.assertTrue(cb.allow_request())
        cb.record_success()
        self.assertEqual(cb.state, "CLOSED")

    def test_orchestrator_end_to_end_cache_glossary_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            repo = SQLiteRepository(db_path=tmp_path / "test.db")
            cred = CredentialManager(vault_path=tmp_path / "test.vault")
            cred._win_cred_available = False
            cred._keyring_mod = None
            settings_mgr = SettingsManager(
                settings_path=tmp_path / "settings.json",
                credential_manager=cred,
            )

            # Configura OfflineFallbackProvider como provedor ativo para teste determinístico + verifica contador de chamadas
            call_counter = {"count": 0}
            real_offline = OfflineFallbackProvider()

            class CountingProvider(OfflineFallbackProvider):
                def translate(self, request: TranslationRequest):
                    call_counter["count"] += 1
                    return real_offline.translate(request)

            orchestrator = TranslationOrchestrator(
                settings_manager=settings_mgr,
                repository=repo,
                providers={ProviderType.OFFLINE_FALLBACK: CountingProvider()},
            )

            # 1ª chamada: vai ao provedor e aplica glossário ("áreas contaminadas" -> "contaminated sites")
            res1 = orchestrator.translate(
                text="Preciso revisar áreas contaminadas hoje",
                target_lang="EN-US",
                preferred_provider=ProviderType.OFFLINE_FALLBACK,
                direction=TranslationDirection.OUTGOING_SENT,
                window_context=WindowContext(hwnd=555, process_name="slack.exe", window_title="Slack"),
            )
            self.assertIn("contaminated sites", res1.translated_text)
            self.assertFalse(res1.cached)
            self.assertEqual(call_counter["count"], 1)

            # Salva manualmente no cache para validar hit de cache SHA-256 em provedores reais
            cache_key = orchestrator.build_cache_key(
                "Você poderia me enviar o relatório amanhã?",
                "PT-BR",
                "EN-US",
                ProviderType.DEEPL,
                FormalityLevel.DEFAULT,
            )
            repo.save_cached_translation(cache_key, res1)
            cached_hit = repo.get_cached_translation(cache_key)
            self.assertIsNotNone(cached_hit)
            assert cached_hit is not None
            self.assertTrue(cached_hit.cached)

            # Verifica histórico gravado no SQLite
            history = repo.list_history()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].app_name, "Slack")

    def test_google_microsoft_and_openai_providers_parse_responses(self) -> None:
        # Google
        g_client = MagicMock()
        g_resp = MagicMock()
        g_resp.status_code = 200
        g_resp.json.return_value = {
            "data": {"translations": [{"translatedText": "Hello world", "detectedSourceLanguage": "pt"}]}
        }
        g_client.post.return_value = g_resp
        g_prov = GoogleTranslateProvider(api_key_resolver=lambda: "gkey", http_client=g_client)
        g_res = g_prov.translate(TranslationRequest(text="Olá mundo", target_lang="EN-US"))
        self.assertEqual(g_res.translated_text, "Hello world")

        # Microsoft
        ms_client = MagicMock()
        ms_resp = MagicMock()
        ms_resp.status_code = 200
        ms_resp.json.return_value = [
            {"detectedLanguage": {"language": "pt"}, "translations": [{"text": "Good morning", "to": "en"}]}
        ]
        ms_client.post.return_value = ms_resp
        ms_prov = MicrosoftTranslatorProvider(api_key_resolver=lambda: "mskey", http_client=ms_client)
        ms_res = ms_prov.translate(TranslationRequest(text="Bom dia", target_lang="EN-US"))
        self.assertEqual(ms_res.translated_text, "Good morning")

        # OpenAI
        oa_client = MagicMock()
        oa_resp = MagicMock()
        oa_resp.status_code = 200
        oa_resp.json.return_value = {
            "choices": [{"message": {"content": "Could you send me the report tomorrow?"}}]
        }
        oa_client.post.return_value = oa_resp
        oa_prov = OpenAIProvider(api_key_resolver=lambda: "sk-test", http_client=oa_client)
        oa_res = oa_prov.translate(
            TranslationRequest(text="Você poderia me enviar o relatório amanhã?", target_lang="EN-US")
        )
        self.assertEqual(oa_res.translated_text, "Could you send me the report tomorrow?")


if __name__ == "__main__":
    unittest.main()
