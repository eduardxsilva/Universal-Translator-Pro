"""Testes Unitários da Integração Oficial DeepL API v2, Tratamento de Erros HTTP e Controle de Consumo."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import httpx

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from universal_translator.application.orchestrator import TranslationOrchestrator
from universal_translator.config.settings_manager import SettingsManager
from universal_translator.domain.entities import TranslationRequest
from universal_translator.domain.enums import DeepLAccountType, FormalityLevel, ProviderType
from universal_translator.infrastructure.persistence.sqlite_repository import SQLiteRepository
from universal_translator.infrastructure.persistence.usage_tracker import UsageTracker
from universal_translator.infrastructure.providers.deepl_provider import DeepLProvider
from universal_translator.infrastructure.providers.resilience import TranslationProviderError
from universal_translator.infrastructure.security.credential_manager import CredentialManager


class DeepLProfessionalTests(unittest.TestCase):
    def test_translate_posts_official_endpoint_and_reports_billed_characters(self) -> None:
        client = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "translations": [{"text": "Hello", "detected_source_language": "PT"}],
            "billed_characters": 6,
        }
        client.post.return_value = response
        provider = DeepLProvider(lambda: "secret:fx", http_client=client)
        result = provider.translate(TranslationRequest(text="Olá!\n", target_lang="EN-US"))
        self.assertEqual(result.translated_text, "Hello")
        self.assertEqual(result.characters_billed, 6)
        self.assertTrue(result.metadata["billed_characters_confirmed"])
        self.assertEqual(client.post.call_args.args[0], "https://api-free.deepl.com/v2/translate")
        self.assertNotIn("secret:fx", repr(result))

    def test_explicit_account_type_free_and_pro_routing(self) -> None:
        client = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "translations": [{"text": "Hello", "detected_source_language": "PT"}],
            "billed_characters": 5,
        }
        client.post.return_value = response

        # Mesmo sem :fx, se configurado como FREE usa api-free.deepl.com
        prov_free = DeepLProvider(
            lambda: "custom-key",
            http_client=client,
            account_type_resolver=lambda: DeepLAccountType.FREE.value,
        )
        prov_free.translate(TranslationRequest(text="Olá", target_lang="EN-US"))
        self.assertEqual(client.post.call_args.args[0], "https://api-free.deepl.com/v2/translate")

        # Configurado como PRO usa api.deepl.com
        prov_pro = DeepLProvider(
            lambda: "custom-key",
            http_client=client,
            account_type_resolver=lambda: DeepLAccountType.PRO.value,
        )
        prov_pro.translate(TranslationRequest(text="Olá", target_lang="EN-US"))
        self.assertEqual(client.post.call_args.args[0], "https://api.deepl.com/v2/translate")

    def test_source_language_is_omitted_for_deepl_auto_detection(self) -> None:
        client = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "translations": [{"text": "Hello", "detected_source_language": "PT"}],
            "billed_characters": 5,
        }
        client.post.return_value = response
        provider = DeepLProvider(lambda: "secret", http_client=client)
        provider.translate(TranslationRequest(text="Olá!", target_lang="EN-US", source_lang=None))
        payload = client.post.call_args.kwargs["json"]
        self.assertNotIn("source_lang", payload)

    def test_usage_endpoint(self) -> None:
        client = MagicMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {"character_count": 17827, "character_limit": 1000000}
        client.get.return_value = response
        usage = DeepLProvider(lambda: "secret", http_client=client).get_usage()
        self.assertEqual(usage["character_count"], 17827)
        self.assertEqual(usage["character_remaining"], 1000000 - 17827)
        self.assertEqual(client.get.call_args.args[0], "https://api.deepl.com/v2/usage")

    def test_http_401_403_429_456_and_503_handling(self) -> None:
        # 429 respeita Retry-After e recupera
        client = MagicMock()
        limited = MagicMock(status_code=429)
        limited.headers = {"Retry-After": "2"}
        ok = MagicMock(status_code=200)
        ok.json.return_value = {
            "translations": [{"text": "Hi", "detected_source_language": "PT"}],
            "billed_characters": 2,
        }
        client.post.side_effect = [limited, ok]
        waits: list[float] = []
        provider = DeepLProvider(lambda: "key", http_client=client, sleep_fn=waits.append)
        provider.translate(TranslationRequest(text="Oi", target_lang="EN-US"))
        self.assertEqual(waits, [2.0])
        self.assertEqual(client.post.call_count, 2)

        # 401, 403, 456 e 503 falham sem repetir POST de tradução
        for code in (401, 403, 456, 503):
            err_client = MagicMock()
            err_resp = MagicMock(status_code=code)
            err_resp.headers = {}
            err_client.post.return_value = err_resp
            with self.assertRaises(TranslationProviderError) as ctx:
                DeepLProvider(lambda: "key", http_client=err_client, sleep_fn=lambda _: None).translate(
                    TranslationRequest(text="Oi", target_lang="EN-US")
                )
            self.assertEqual(ctx.exception.status_code, code)
            self.assertEqual(err_client.post.call_count, 1)

    def test_connect_error_retries_with_exponential_backoff_and_jitter(self) -> None:
        client = MagicMock()
        ok = MagicMock(status_code=200)
        ok.json.return_value = {
            "translations": [{"text": "Hello", "detected_source_language": "PT"}],
            "billed_characters": 5,
        }
        client.post.side_effect = [httpx.ConnectError("Connection reset"), ok]
        waits: list[float] = []
        provider = DeepLProvider(
            lambda: "key:fx", http_client=client, max_retries=2, sleep_fn=waits.append
        )
        res = provider.translate(TranslationRequest(text="Olá", target_lang="EN-US"))
        self.assertEqual(res.translated_text, "Hello")
        self.assertEqual(len(waits), 1)
        self.assertGreaterEqual(waits[0], 0.4)

    def test_concurrent_deduplication_prevents_duplicate_deepl_requests(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = SQLiteRepository(db_path=tmp / "dedup.db")
            cred = CredentialManager(vault_path=tmp / "dedup.vault")
            cred._win_cred_available = False
            cred._keyring_mod = None
            sm = SettingsManager(settings_path=tmp / "settings.json", credential_manager=cred)
            tracker = UsageTracker(tmp / "usage.db")

            api_calls = 0
            barrier = threading.Barrier(2)

            class SlowDeepLProvider(DeepLProvider):
                def translate(self, request: TranslationRequest):
                    nonlocal api_calls
                    api_calls += 1
                    return super().translate(request)

            client = MagicMock()
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "translations": [
                    {
                        "text": "Hello, how are you? I would like to know if you received the document I sent yesterday.",
                        "detected_source_language": "PT",
                    }
                ],
                "billed_characters": 87,
            }
            client.post.return_value = resp
            prov = SlowDeepLProvider(lambda: "test:fx", http_client=client)

            orch = TranslationOrchestrator(
                settings_manager=sm,
                repository=repo,
                usage_tracker=tracker,
                providers={ProviderType.DEEPL: prov},
            )

            results = []

            def _worker():
                barrier.wait()
                r = orch.translate(
                    "Olá, tudo bem? Gostaria de saber se você recebeu o documento que enviei ontem.",
                    target_lang="EN-US",
                    preferred_provider=ProviderType.DEEPL,
                )
                results.append(r)

            t1 = threading.Thread(target=_worker)
            t2 = threading.Thread(target=_worker)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual(len(results), 2)
            self.assertEqual(api_calls, 1, "Requisições simultâneas para o mesmo texto devem ser deduplicadas")
            self.assertTrue(any(r.cached for r in results))

    def test_usage_tracker_persists_and_separates_estimate_confirmation_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tracker = UsageTracker(Path(td) / "usage.db")
            tracker.record(100, confirmed=False, source="local_estimate")
            tracker.record(90, confirmed=True, source="deepl_api")
            tracker.record(50, confirmed=False, source="cache_hit", cached=True)
            tracker.record_api_snapshot(12500, 500000, account_tier="free")

            reopened = UsageTracker(Path(td) / "usage.db")
            summary = reopened.summary()
            self.assertEqual(summary["estimated_characters"], 100)
            self.assertEqual(summary["confirmed_characters"], 90)
            self.assertEqual(summary["saved_by_cache_characters"], 50)
            self.assertEqual(summary["api_character_count"], 12500)
            self.assertEqual(summary["api_character_limit"], 500000)
            self.assertEqual(summary["api_character_remaining"], 487500)
            self.assertEqual(len(reopened.list_events()), 3)


if __name__ == "__main__":
    unittest.main()
