"""Sistema de observabilidade, diagnóstico e logs rotativos com sanitização de dados sensíveis."""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from ...domain.enums import LogCategory


@dataclass
class DiagnosticRecord:
    timestamp: float
    level: str
    category: str
    operation_id: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class PrivacyRedactingFilter(logging.Filter):
    """Remove automaticamente chaves de API, tokens Bearer, headers de autorização e textos privados dos logs."""

    _PATTERNS = [
        (re.compile(r"(DeepL-Auth-Key\s+)[^\s\"',]+", re.IGNORECASE), r"\1[REDACTED_DEEPL_KEY]"),
        (re.compile(r"(Bearer\s+)[A-Za-z0-9\-\._~\+\/]+=*", re.IGNORECASE), r"\1[REDACTED_BEARER_TOKEN]"),
        (re.compile(r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}:fx)", re.IGNORECASE), "[REDACTED_DEEPL_FX_KEY]"),
        (re.compile(r"(sk-[A-Za-z0-9\-_]{16,})"), "[REDACTED_OPENAI_KEY]"),
        (re.compile(r"(api[_-]?key\s*[:=]\s*['\"]?)[^'\"\s,;]+", re.IGNORECASE), r"\1[REDACTED_API_KEY]"),
        (re.compile(r"(password\s*[:=]\s*['\"]?)[^'\"\s,;]+", re.IGNORECASE), r"\1[REDACTED_PASSWORD]"),
    ]

    @classmethod
    def sanitize_text(cls, value: str) -> str:
        if not value:
            return ""
        sanitized = str(value)
        for pattern, replacement in cls._PATTERNS:
            sanitized = pattern.sub(replacement, sanitized)
        return sanitized

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.sanitize_text(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self.sanitize_text(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


class ObservabilityLogger:
    """Gerenciador central de logs categorizados, rotativos e auditáveis para tela de Diagnóstico."""

    _instance: ObservabilityLogger | None = None
    _lock = threading.RLock()

    def __init__(self, log_dir: Path | None = None) -> None:
        if log_dir is None:
            from ...config.settings_manager import get_app_data_dir
            log_dir = get_app_data_dir() / "logs"

        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "universal_translator.log"
        self._records: list[DiagnosticRecord] = []
        self._max_in_memory = 500
        self._redactor = PrivacyRedactingFilter()

        self._logger = logging.getLogger("UniversalTranslator")
        self._logger.setLevel(logging.DEBUG)
        for existing_handler in list(self._logger.handlers):
            try:
                existing_handler.close()
            except Exception:
                pass
        self._logger.handlers.clear()
        self._logger.addFilter(self._redactor)

        file_handler = RotatingFileHandler(
            self.log_file,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(category)-18s | %(operation_id)-16s | %(message)s"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(self._redactor)
        self._logger.addHandler(file_handler)

    @classmethod
    def get_instance(cls, log_dir: Path | None = None) -> ObservabilityLogger:
        with cls._lock:
            if cls._instance is None or (log_dir is not None and cls._instance.log_dir != log_dir):
                cls._instance = cls(log_dir=log_dir)
            return cls._instance

    def log(
        self,
        level: int,
        category: LogCategory,
        message: str,
        operation_id: str = "system",
        details: dict[str, Any] | None = None,
    ) -> None:
        safe_msg = self._redactor.sanitize_text(message)
        safe_details: dict[str, Any] = {}
        if details:
            for k, v in details.items():
                if k.lower() in ("api_key", "password", "secret", "raw_text", "source_text", "translated_text"):
                    safe_details[k] = f"[REDACTED_LEN_{len(str(v))}]"
                else:
                    safe_details[k] = self._redactor.sanitize_text(str(v))

        extra = {
            "category": category.value,
            "operation_id": operation_id,
        }
        full_msg = safe_msg
        if safe_details:
            full_msg = f"{safe_msg} | meta={json.dumps(safe_details, ensure_ascii=False)}"

        with self._lock:
            self._logger.log(level, full_msg, extra=extra)
            rec = DiagnosticRecord(
                timestamp=time.time(),
                level=logging.getLevelName(level),
                category=category.value,
                operation_id=operation_id,
                message=safe_msg,
                details=safe_details,
            )
            self._records.append(rec)
            if len(self._records) > self._max_in_memory:
                self._records = self._records[-self._max_in_memory :]

    def info(
        self,
        category: LogCategory,
        message: str,
        operation_id: str = "system",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.log(logging.INFO, category, message, operation_id, details)

    def warning(
        self,
        category: LogCategory,
        message: str,
        operation_id: str = "system",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.log(logging.WARNING, category, message, operation_id, details)

    def error(
        self,
        category: LogCategory,
        message: str,
        operation_id: str = "system",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.log(logging.ERROR, category, message, operation_id, details)

    def get_recent_records(
        self, category: str | None = None, level: str | None = None, limit: int = 200
    ) -> list[DiagnosticRecord]:
        with self._lock:
            items = list(self._records)
        if category and category != "TODOS":
            items = [r for r in items if r.category == category]
        if level and level != "TODOS":
            items = [r for r in items if r.level == level]
        return items[-limit:]

    def export_logs(self, destination_path: Path) -> Path:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_file.exists():
            shutil.copy2(self.log_file, destination_path)
        else:
            destination_path.write_text("", encoding="utf-8")
        return destination_path
