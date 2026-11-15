"""Orquestrador da Camada de Aplicação com integração exclusiva DeepL, cache SQLite, deduplicação e controle de consumo."""
from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict
from typing import Any

from ..config.settings_manager import AppSettings, SettingsManager
from ..domain.contracts import (
    PersistenceRepositoryPort,
    TextCapturePort,
    TranslationProviderPort,
)
from ..domain.entities import (
    ApplicationProfile,
    CaptureResult,
    ConversationTurn,
    GlossaryEntry,
    ProviderMetrics,
    ReplacementResult,
    TranslationHistoryEntry,
    TranslationRequest,
    TranslationResult,
    WindowContext,
    generate_operation_id,
)
from ..domain.enums import (
    FormalityLevel,
    LogCategory,
    ProviderType,
    TranslationDirection,
)
from ..domain.services import GlossaryEngine, LanguageDetector, ProfileMatcher
from ..infrastructure.clipboard.safe_clipboard_engine import SafeTextCaptureEngine
from ..infrastructure.logging.logger import ObservabilityLogger
from ..infrastructure.persistence.sqlite_repository import SQLiteRepository
from ..infrastructure.persistence.usage_tracker import UsageTracker
from ..infrastructure.providers import (
    CircuitBreaker,
    DeepLProvider,
    GoogleGTXFreeProvider,
    GoogleTranslateProvider,
    MicrosoftTranslatorProvider,
    OfflineFallbackProvider,
    OpenAIProvider,
    RetryPolicy,
    TranslationProviderError,
)
from ..infrastructure.windows.startup_manager import WindowsStartupManager
from .auto_monitor import AutoTranslationMonitor


class TranslationOrchestrator:
    """Coordena todos os casos de uso do Universal Translator Pro de forma desacoplada da interface gráfica.

    Quando `DEEPL` está selecionada (padrão oficial do produto):
    - Utiliza exclusivamente a API REST oficial da DeepL (`/v2/translate` e `/v2/usage`).
    - Não recorre silenciosamente a tradutores alternativos em caso de falha ou falta de chave.
    - Deduplica requisições concorrentes idênticas e armazena resultados no cache SQLite persistente.
    - Registra consumo diferenciando valores confirmados pela DeepL de estimativas locais.
    """

    def __init__(
        self,
        settings_manager: SettingsManager | None = None,
        repository: PersistenceRepositoryPort | None = None,
        capture_engine: TextCapturePort | None = None,
        logger: ObservabilityLogger | None = None,
        providers: dict[ProviderType, TranslationProviderPort] | None = None,
        usage_tracker: UsageTracker | None = None,
        startup_manager: WindowsStartupManager | None = None,
    ) -> None:
        self.logger = logger or ObservabilityLogger.get_instance()
        self.settings_manager = settings_manager or SettingsManager()
        self.settings: AppSettings = self.settings_manager.load()
        self.repository = repository or SQLiteRepository()
        self.usage_tracker = usage_tracker or UsageTracker()
        self.startup_manager = startup_manager or WindowsStartupManager()
        self.capture_engine = capture_engine or SafeTextCaptureEngine(logger=self.logger)

        self.glossary_engine = GlossaryEngine(self.repository.list_glossary())
        self.profile_matcher = ProfileMatcher(self.repository.list_profiles())

        self._has_custom_providers = providers is not None
        self._providers: dict[ProviderType, TranslationProviderPort] = (
            providers or self._build_default_providers()
        )
        self._gtx_free_provider = GoogleGTXFreeProvider(
            timeout_seconds=self.settings.request_timeout_seconds
        )
        self._circuit_breakers: dict[ProviderType, CircuitBreaker] = {
            p: CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=30.0)
            for p in ProviderType
        }
        self._metrics: dict[ProviderType, ProviderMetrics] = {
            p: ProviderMetrics(provider=p.value) for p in ProviderType
        }
        self._conversation_memory: dict[str, list[ConversationTurn]] = defaultdict(list)
        self._in_flight_lock = threading.RLock()
        self._inflight_translation_locks: dict[str, tuple[threading.Lock, int]] = {}
        self._last_capture: CaptureResult | None = None
        self.auto_monitor = AutoTranslationMonitor(
            orchestrator=self,
            idle_timeout_seconds=float(self.settings.auto_idle_timeout_seconds),
        )

    def _build_default_providers(self) -> dict[ProviderType, TranslationProviderPort]:
        return {
            ProviderType.DEEPL: DeepLProvider(
                api_key_resolver=lambda: self.settings_manager.get_provider_api_key(ProviderType.DEEPL),
                timeout_seconds=self.settings.request_timeout_seconds,
                max_retries=self.settings.max_retries,
                account_type_resolver=lambda: self.settings.deepl_account_type,
            ),
            ProviderType.GOOGLE: GoogleTranslateProvider(
                api_key_resolver=lambda: self.settings_manager.get_provider_api_key(ProviderType.GOOGLE),
                timeout_seconds=self.settings.request_timeout_seconds,
            ),
            ProviderType.MICROSOFT: MicrosoftTranslatorProvider(
                api_key_resolver=lambda: self.settings_manager.get_provider_api_key(ProviderType.MICROSOFT),
                region_resolver=lambda: self.settings.microsoft_region,
                timeout_seconds=self.settings.request_timeout_seconds,
            ),
            ProviderType.OPENAI: OpenAIProvider(
                api_key_resolver=lambda: self.settings_manager.get_provider_api_key(ProviderType.OPENAI),
                model_resolver=lambda: self.settings.openai_model,
                timeout_seconds=max(15.0, self.settings.request_timeout_seconds),
            ),
            ProviderType.OFFLINE_FALLBACK: OfflineFallbackProvider(),
        }

    def reload_domain_rules(self) -> None:
        self.settings = self.settings_manager.load()
        self.glossary_engine.set_entries(self.repository.list_glossary())
        self.profile_matcher.set_profiles(self.repository.list_profiles())
        self.auto_monitor.set_idle_timeout_seconds(float(self.settings.auto_idle_timeout_seconds))

    def capture_from_active_window(
        self, target_hwnd: int | None = None
    ) -> tuple[CaptureResult, ApplicationProfile]:
        result = self.capture_engine.capture_selection(target_hwnd=target_hwnd)
        profile = self.profile_matcher.match(result.window_context)
        if result.window_context is not None:
            result.window_context.matched_profile_name = profile.app_name
        if result.success:
            self._last_capture = result
        return result, profile

    def capture_entire_field_from_active_window(
        self, target_hwnd: int | None = None
    ) -> tuple[CaptureResult, ApplicationProfile]:
        if hasattr(self.capture_engine, "capture_entire_field"):
            result = self.capture_engine.capture_entire_field(
                target_hwnd=target_hwnd,
                prefer_uia=self.settings.prefer_uia_capture,
                allow_clipboard_fallback=self.settings.allow_clipboard_fallback,
            )
        else:
            result = self.capture_engine.capture_selection(target_hwnd=target_hwnd)
        profile = self.profile_matcher.match(result.window_context)
        if result.window_context is not None:
            result.window_context.matched_profile_name = profile.app_name
        if result.success:
            self._last_capture = result
        return result, profile

    @property
    def last_capture(self) -> CaptureResult | None:
        return self._last_capture

    def set_manual_capture(
        self, text: str, hwnd: int | None = None, app_name: str = "Padrão Global"
    ) -> CaptureResult:
        from ..domain.enums import CaptureMethod

        ctx = WindowContext(
            hwnd=hwnd or 0,
            window_title=app_name,
            matched_profile_name=app_name,
        )
        cap = CaptureResult(
            success=bool(text and text.strip()),
            text=text,
            window_context=ctx,
            method=CaptureMethod.MANUAL_BUFFER,
        )
        self._last_capture = cap
        return cap

    @staticmethod
    def build_cache_key(
        prepared_text: str,
        source_lang: str,
        target_lang: str,
        provider: ProviderType,
        formality: FormalityLevel,
        context_fingerprint: str = "",
    ) -> str:
        norm_text = prepared_text.strip().replace("\r\n", "\n")
        src_norm = (source_lang or "AUTO").strip().upper().split("-")[0]
        tgt_norm = (target_lang or "EN-US").strip().upper()
        raw = f"{provider.value}|{src_norm}|{tgt_norm}|{formality.value}|{context_fingerprint}|{norm_text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def translate(
        self,
        text: str,
        target_lang: str | None = None,
        source_lang: str | None = None,
        direction: TranslationDirection = TranslationDirection.OUTGOING_SENT,
        window_context: WindowContext | None = None,
        preferred_provider: ProviderType | None = None,
        formality: FormalityLevel | None = None,
        privacy_no_history: bool | None = None,
        bypass_cache: bool = False,
    ) -> TranslationResult:
        """Deduplica traduções simultâneas idênticas; a segunda consulta aproveita o cache persistente."""
        norm_input = (text or "").strip().replace("\r\n", "\n")
        norm_src = (source_lang or "AUTO").strip().upper().split("-")[0]
        norm_tgt = (target_lang or self.settings.target_language_code or "EN-US").strip().upper()
        fingerprint = hashlib.sha256(
            f"{norm_input}|{norm_src}|{norm_tgt}|{preferred_provider}|{formality}".encode("utf-8")
        ).hexdigest()
        with self._in_flight_lock:
            entry = self._inflight_translation_locks.get(fingerprint)
            if entry is None:
                lock = threading.Lock()
                self._inflight_translation_locks[fingerprint] = (lock, 1)
            else:
                lock, references = entry
                self._inflight_translation_locks[fingerprint] = (lock, references + 1)
        try:
            with lock:
                return self._translate_impl(
                    text,
                    target_lang,
                    source_lang,
                    direction,
                    window_context,
                    preferred_provider,
                    formality,
                    privacy_no_history,
                    bypass_cache,
                )
        finally:
            with self._in_flight_lock:
                current = self._inflight_translation_locks.get(fingerprint)
                if current and current[0] is lock:
                    if current[1] <= 1:
                        self._inflight_translation_locks.pop(fingerprint, None)
                    else:
                        self._inflight_translation_locks[fingerprint] = (lock, current[1] - 1)

    def _translate_impl(
        self,
        text: str,
        target_lang: str | None = None,
        source_lang: str | None = None,
        direction: TranslationDirection = TranslationDirection.OUTGOING_SENT,
        window_context: WindowContext | None = None,
        preferred_provider: ProviderType | None = None,
        formality: FormalityLevel | None = None,
        privacy_no_history: bool | None = None,
        bypass_cache: bool = False,
    ) -> TranslationResult:
        op_id = generate_operation_id("trn")
        clean_text = (text or "").strip().replace("\r\n", "\n")
        if not clean_text:
            raise ValueError("Não há texto para traduzir.")

        profile = self.profile_matcher.match(window_context)
        explicit_auto = source_lang is None or str(source_lang).strip().upper() in ("AUTO", "ANY", "")
        detected_src = (
            LanguageDetector.detect(clean_text)
            if explicit_auto
            else str(source_lang).strip().upper()
        )

        if target_lang:
            resolved_target = target_lang
        elif direction == TranslationDirection.INCOMING_RECEIVED:
            resolved_target = (
                profile.incoming_target_lang
                or self.settings.incoming_language_code
                or "PT-BR"
            )
        else:
            prof_target = (
                profile.target_lang
                or self.settings.target_language_code
                or "EN-US"
            )
            if detected_src.split("-")[0].upper() == prof_target.split("-")[0].upper():
                resolved_target = profile.incoming_target_lang or "PT-BR"
            else:
                resolved_target = prof_target

        resolved_formality = (
            formality
            or profile.formality
            or FormalityLevel(self.settings.formality)
        )
        resolved_provider = preferred_provider or profile.preferred_provider
        try:
            if preferred_provider is None and self.settings.active_provider:
                resolved_provider = ProviderType(self.settings.active_provider)
        except ValueError:
            resolved_provider = ProviderType.DEEPL

        no_history = (
            privacy_no_history
            if privacy_no_history is not None
            else self.settings.privacy_no_history
        )

        glossary_res = self.glossary_engine.pre_process(
            clean_text,
            source_lang=detected_src,
            target_lang=resolved_target,
            domain_tag=profile.custom_glossary_domain,
        )

        conv_key = self._conversation_key(window_context, profile)
        context_turns = (
            list(self._conversation_memory.get(conv_key, []))[-3:]
            if profile.preserve_context
            else []
        )
        # Chave de cache baseada em texto normalizado, idioma de origem, idioma de destino e parâmetros (Seção 5)
        cache_key = self.build_cache_key(
            glossary_res.prepared_text,
            detected_src,
            resolved_target,
            resolved_provider,
            resolved_formality,
            "",
        )
        if not bypass_cache:
            cached_res = self.repository.get_cached_translation(cache_key)
            if cached_res is not None:
                final_cached_text = self.glossary_engine.post_process(
                    cached_res.translated_text, glossary_res.token_map
                )
                cached_res.translated_text = final_cached_text
                cached_res.source_text = clean_text
                cached_res.glossary_matches = glossary_res.applied_terms
                cached_res.direction = direction
                cached_res.app_profile_name = profile.app_name
                cached_res.operation_id = op_id

                m = self._metrics[resolved_provider]
                m.total_requests += 1
                m.cache_hits += 1

                if resolved_provider == ProviderType.DEEPL:
                    self.usage_tracker.record(
                        len(clean_text),
                        confirmed=False,
                        source="cache_hit",
                        cached=True,
                        source_lang=detected_src,
                        target_lang=resolved_target,
                    )

                if not no_history:
                    self._record_history(cached_res)
                self._record_conversation_turn(
                    window_context,
                    profile,
                    clean_text,
                    final_cached_text,
                    detected_src,
                    resolved_target,
                    direction,
                )
                return cached_res

        request = TranslationRequest(
            text=glossary_res.prepared_text,
            source_lang=(
                None
                if (resolved_provider == ProviderType.DEEPL and explicit_auto)
                else (source_lang if resolved_provider == ProviderType.DEEPL else detected_src)
            ),
            target_lang=resolved_target,
            provider=resolved_provider,
            formality=resolved_formality,
            direction=direction,
            app_profile_name=profile.app_name,
            glossary_domain=profile.custom_glossary_domain,
            context_turns=context_turns,
            privacy_no_history=no_history,
            bypass_cache=bypass_cache,
            operation_id=op_id,
        )

        provider_result = self._execute_with_resilience(
            request, primary_provider=resolved_provider
        )
        if provider_result.provider == ProviderType.DEEPL:
            confirmed = bool(provider_result.metadata.get("billed_characters_confirmed"))
            self.usage_tracker.record(
                provider_result.characters_billed or len(glossary_res.prepared_text),
                confirmed=confirmed,
                source="deepl_api",
                cached=False,
                source_lang=provider_result.detected_source_lang or detected_src,
                target_lang=resolved_target,
            )

        final_text = self.glossary_engine.post_process(
            provider_result.translated_text, glossary_res.token_map
        )

        if provider_result.provider != ProviderType.OFFLINE_FALLBACK:
            ttl_sec = max(1, self.settings.cache_ttl_days) * 86400
            self.repository.save_cached_translation(
                cache_key, provider_result, ttl_seconds=ttl_sec
            )
            alt_key = self.build_cache_key(
                glossary_res.prepared_text,
                provider_result.detected_source_lang or detected_src,
                resolved_target,
                resolved_provider,
                resolved_formality,
                "",
            )
            if alt_key != cache_key:
                self.repository.save_cached_translation(
                    alt_key, provider_result, ttl_seconds=ttl_sec
                )

        provider_result.translated_text = final_text
        provider_result.source_text = clean_text
        provider_result.glossary_matches = glossary_res.applied_terms
        provider_result.direction = direction
        provider_result.app_profile_name = profile.app_name
        provider_result.operation_id = op_id

        if not no_history:
            self._record_history(provider_result)

        self._record_conversation_turn(
            window_context,
            profile,
            clean_text,
            final_text,
            provider_result.detected_source_lang,
            resolved_target,
            direction,
        )

        return provider_result

    def _execute_with_resilience(
        self,
        request: TranslationRequest,
        primary_provider: ProviderType,
    ) -> TranslationResult:
        candidates: list[ProviderType] = [primary_provider]
        deepl_only = primary_provider == ProviderType.DEEPL
        try:
            fb = ProviderType(self.settings.fallback_provider)
            if (
                not deepl_only
                and fb not in candidates
                and fb in self._providers
                and self._providers[fb].is_configured()
            ):
                candidates.append(fb)
        except ValueError:
            pass

        last_exc: Exception | None = None
        retry_policy = RetryPolicy(max_retries=self.settings.max_retries)

        for prov_type in candidates:
            provider = self._providers.get(prov_type)
            if provider is None:
                continue

            if not self._has_custom_providers and not provider.is_configured():
                continue

            cb = self._circuit_breakers[prov_type]
            metric = self._metrics[prov_type]
            metric.circuit_state = cb.state

            if not cb.allow_request():
                continue

            try:
                metric.total_requests += 1
                result = retry_policy.execute(lambda p=provider: p.translate(request))
                cb.record_success()
                metric.circuit_state = cb.state
                metric.total_characters += result.characters_billed
                metric.estimated_cost_usd = round(
                    metric.estimated_cost_usd + result.estimated_cost_usd, 6
                )
                if metric.avg_latency_ms <= 0:
                    metric.avg_latency_ms = result.latency_ms
                else:
                    metric.avg_latency_ms = round(
                        (metric.avg_latency_ms * 0.7) + (result.latency_ms * 0.3), 2
                    )
                return result
            except Exception as exc:
                last_exc = exc
                metric.failed_requests += 1
                metric.last_error = str(exc)
                if isinstance(exc, TranslationProviderError) and not exc.auth_failed:
                    cb.record_failure(str(exc))
                metric.circuit_state = cb.state
                self.logger.error(
                    LogCategory.PROVIDER,
                    f"Erro no provedor {prov_type.value}: {exc}",
                    operation_id=request.operation_id,
                )

        if not self._has_custom_providers and not deepl_only:
            try:
                res = self._gtx_free_provider.translate(request)
                self.logger.info(
                    LogCategory.PROVIDER,
                    f"Tradução realizada via Google GTX Automático em {res.latency_ms:.0f}ms.",
                    operation_id=request.operation_id,
                )
                return res
            except Exception as gtx_exc:
                self.logger.warning(
                    LogCategory.PROVIDER,
                    f"Fallback GTX indisponível ({gtx_exc}); acionando OfflineFallbackProvider.",
                    operation_id=request.operation_id,
                )
                offline = (
                    self._providers.get(ProviderType.OFFLINE_FALLBACK)
                    or OfflineFallbackProvider()
                )
                return offline.translate(request)

        if last_exc is not None:
            raise last_exc
        if deepl_only:
            raise TranslationProviderError(
                "DeepL selecionada, mas não configurada. Cadastre sua API Key na seção DeepL para traduzir.",
                provider=ProviderType.DEEPL,
                auth_failed=True,
            )
        raise RuntimeError("Nenhum provedor de tradução pôde processar a solicitação.")

    def translate_and_replace(
        self,
        capture: CaptureResult | None = None,
        target_lang: str | None = None,
        direction: TranslationDirection = TranslationDirection.OUTGOING_SENT,
    ) -> tuple[TranslationResult, ReplacementResult]:
        active_capture = capture or self._last_capture
        if (
            active_capture is None
            or not active_capture.success
            or not active_capture.text.strip()
        ):
            raise ValueError("Nenhum texto capturado disponível para tradução.")

        translation = self.translate(
            text=active_capture.text,
            target_lang=target_lang,
            direction=direction,
            window_context=active_capture.window_context,
        )

        if active_capture.window_context is None:
            from ..domain.enums import ReplacementSafetyStatus

            rep = ReplacementResult(
                success=False,
                safety_status=ReplacementSafetyStatus.WINDOW_INVALID,
                method_used=active_capture.method,
                original_text=active_capture.text,
                new_text=translation.translated_text,
                hwnd=None,
                rollback_available=False,
                message="Janela de origem não identificada; tradução disponível para cópia.",
            )
            return translation, rep

        replacement = self.capture_engine.replace_text_safely(
            new_text=translation.translated_text,
            original_context=active_capture.window_context,
            expected_original_text=active_capture.text,
            verify_content_before_paste=self.settings.verify_before_replace,
        )
        return translation, replacement

    def rollback_last_replacement(self) -> ReplacementResult:
        return self.capture_engine.rollback_last_replacement()

    def _record_history(self, result: TranslationResult) -> None:
        entry = TranslationHistoryEntry(
            operation_id=result.operation_id or generate_operation_id("hst"),
            source_text=result.source_text,
            translated_text=result.translated_text,
            source_lang=result.detected_source_lang,
            target_lang=result.target_lang,
            provider=result.provider.value,
            app_name=result.app_profile_name,
            direction=result.direction.value,
            cached=result.cached,
            characters_count=len(result.source_text),
            estimated_cost_usd=result.estimated_cost_usd,
            latency_ms=result.latency_ms,
            created_at=time.time(),
        )
        self.repository.add_history(entry)

    @staticmethod
    def _conversation_key(
        context: WindowContext | None, profile: ApplicationProfile
    ) -> str:
        if context and context.hwnd:
            return f"hwnd:{context.hwnd}:{profile.app_name}"
        return f"app:{profile.app_name}"

    def _record_conversation_turn(
        self,
        context: WindowContext | None,
        profile: ApplicationProfile,
        source_text: str,
        translated_text: str,
        source_lang: str,
        target_lang: str,
        direction: TranslationDirection,
    ) -> None:
        key = self._conversation_key(context, profile)
        turns = self._conversation_memory[key]
        turns.append(
            ConversationTurn(
                source_text=source_text,
                translated_text=translated_text,
                source_lang=source_lang,
                target_lang=target_lang,
                direction=direction,
            )
        )
        if len(turns) > 10:
            self._conversation_memory[key] = turns[-10:]

    def clear_conversation_context(self) -> None:
        self._conversation_memory.clear()

    def test_deepl_connection(self) -> dict[str, Any]:
        provider = self._providers.get(ProviderType.DEEPL)
        if not isinstance(provider, DeepLProvider):
            raise RuntimeError("Provedor DeepL não está disponível.")
        usage = provider.test_connection()
        self.usage_tracker.record_api_snapshot(
            character_count=int(usage.get("character_count", 0)),
            character_limit=int(usage.get("character_limit", 0)),
            account_tier=str(usage.get("account_tier", "auto")),
        )
        return usage

    def get_local_usage_summary(self) -> dict[str, int]:
        return self.usage_tracker.summary()

    def get_provider_metrics(self) -> list[ProviderMetrics]:
        for p, cb in self._circuit_breakers.items():
            self._metrics[p].circuit_state = cb.state
        return list(self._metrics.values())
