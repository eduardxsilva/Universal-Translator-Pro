"""Mecanismos de resiliência: Circuit Breaker, Backoff Exponencial com Jitter, Deduplicação e Controle de Custos."""
from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from ...domain.enums import ProviderType

T = TypeVar("T")


class TranslationProviderError(RuntimeError):
    """Erro padronizado de provedores de tradução com classificação de retentativa."""

    def __init__(
        self,
        message: str,
        provider: ProviderType,
        status_code: int | None = None,
        retryable: bool = False,
        quota_exceeded: bool = False,
        auth_failed: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.quota_exceeded = quota_exceeded
        self.auth_failed = auth_failed


@dataclass
class CircuitBreakerState:
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 30.0
    failure_count: int = 0
    state: str = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
    last_failure_time: float = 0.0
    last_error: str = ""


class CircuitBreaker:
    """Protege contra falhas em cascata quando um provedor de tradução está indisponível."""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
    ) -> None:
        self._lock = threading.RLock()
        self._state = CircuitBreakerState(
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout_seconds,
        )

    @property
    def state(self) -> str:
        with self._lock:
            if self._state.state == "OPEN":
                elapsed = time.monotonic() - self._state.last_failure_time
                if elapsed >= self._state.recovery_timeout_seconds:
                    self._state.state = "HALF_OPEN"
            return self._state.state

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._state.last_error

    def allow_request(self) -> bool:
        current = self.state
        return current in ("CLOSED", "HALF_OPEN")

    def record_success(self) -> None:
        with self._lock:
            self._state.failure_count = 0
            self._state.state = "CLOSED"
            self._state.last_error = ""

    def record_failure(self, error_msg: str = "") -> None:
        with self._lock:
            self._state.failure_count += 1
            self._state.last_failure_time = time.monotonic()
            self._state.last_error = error_msg
            if self._state.failure_count >= self._state.failure_threshold:
                self._state.state = "OPEN"

    def reset(self) -> None:
        with self._lock:
            self._state.failure_count = 0
            self._state.state = "CLOSED"
            self._state.last_error = ""


class RetryPolicy:
    """Backoff exponencial com jitter que NUNCA repete operações que gerem cobrança duplicada ou erros de autenticação/cota."""

    def __init__(
        self,
        max_retries: int = 2,
        base_delay_seconds: float = 0.35,
        max_delay_seconds: float = 3.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_retries = max(0, max_retries)
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self._sleep = sleep_fn

    def execute(self, operation: Callable[[], T]) -> T:
        attempt = 0
        while True:
            try:
                return operation()
            except TranslationProviderError as exc:
                # Nunca faz retry se não for explicitamente seguro/retryable (evita custos duplicados e bloqueio de conta)
                if not exc.retryable or exc.auth_failed or exc.quota_exceeded or attempt >= self.max_retries:
                    raise
                delay = min(
                    self.max_delay_seconds,
                    self.base_delay_seconds * (2**attempt) + random.uniform(0.01, 0.12),
                )
                self._sleep(delay)
                attempt += 1


class CostEstimator:
    """Calcula estimativa de custo por milhão de caracteres conforme tabela comercial de cada provedor."""

    # Preços médios comerciais por 1.000.000 de caracteres (USD)
    PRICE_PER_MILLION_CHARS: dict[ProviderType, float] = {
        ProviderType.DEEPL: 25.00,
        ProviderType.GOOGLE: 20.00,
        ProviderType.MICROSOFT: 10.00,
        ProviderType.OPENAI: 3.50,  # gpt-4o-mini equivalente por caracteres
        ProviderType.OFFLINE_FALLBACK: 0.0,
    }

    @classmethod
    def estimate_usd(cls, provider: ProviderType, char_count: int, is_free_tier: bool = False) -> float:
        if is_free_tier or char_count <= 0:
            return 0.0
        rate = cls.PRICE_PER_MILLION_CHARS.get(provider, 20.0)
        return round((char_count / 1_000_000.0) * rate, 6)
