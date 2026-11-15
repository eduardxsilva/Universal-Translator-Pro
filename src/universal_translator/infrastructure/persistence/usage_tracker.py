"""Registro local de consumo: estimativas locais, confirmações de resposta e snapshots de utilização da API DeepL."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ...config.settings_manager import get_app_data_dir


class UsageTracker:
    """Rastreia caracteres consumidos diferenciando estimativas locais de valores confirmados pela DeepL.

    Não presume renovação mensal automática: preserva o saldo exato retornado por `/v2/usage`.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.path = db_path or (get_app_data_dir() / "deepl_usage.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    characters INTEGER NOT NULL,
                    confirmed INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    cached INTEGER NOT NULL DEFAULT 0,
                    source_lang TEXT NOT NULL DEFAULT '',
                    target_lang TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at DESC);

                CREATE TABLE IF NOT EXISTS api_usage_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checked_at REAL NOT NULL,
                    character_count INTEGER NOT NULL,
                    character_limit INTEGER NOT NULL,
                    account_tier TEXT NOT NULL DEFAULT 'auto'
                );
                CREATE INDEX IF NOT EXISTS idx_snapshot_checked ON api_usage_snapshots(checked_at DESC);
                """
            )
            # Garante colunas novas em bancos pré-existentes
            cols = {
                row["name"]
                for row in db.execute("PRAGMA table_info(usage_events)").fetchall()
            }
            if "cached" not in cols:
                db.execute("ALTER TABLE usage_events ADD COLUMN cached INTEGER NOT NULL DEFAULT 0")
            if "source_lang" not in cols:
                db.execute("ALTER TABLE usage_events ADD COLUMN source_lang TEXT NOT NULL DEFAULT ''")
            if "target_lang" not in cols:
                db.execute("ALTER TABLE usage_events ADD COLUMN target_lang TEXT NOT NULL DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db

    def record(
        self,
        characters: int,
        confirmed: bool = False,
        source: str = "translation",
        cached: bool = False,
        source_lang: str = "",
        target_lang: str = "",
    ) -> None:
        if characters <= 0:
            return
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO usage_events(
                    created_at, characters, confirmed, source, cached, source_lang, target_lang
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    int(characters),
                    1 if confirmed else 0,
                    source,
                    1 if cached else 0,
                    source_lang,
                    target_lang,
                ),
            )
            db.commit()

    def record_api_snapshot(
        self,
        character_count: int,
        character_limit: int,
        account_tier: str = "auto",
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO api_usage_snapshots(
                    checked_at, character_count, character_limit, account_tier
                ) VALUES (?, ?, ?, ?)
                """,
                (time.time(), max(0, int(character_count)), max(0, int(character_limit)), account_tier),
            )
            db.commit()

    def get_latest_api_snapshot(self) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                """
                SELECT checked_at, character_count, character_limit, account_tier
                FROM api_usage_snapshots
                ORDER BY checked_at DESC LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            used = int(row["character_count"])
            limit = int(row["character_limit"])
            return {
                "checked_at": float(row["checked_at"]),
                "character_count": used,
                "character_limit": limit,
                "character_remaining": max(0, limit - used) if limit > 0 else 0,
                "account_tier": str(row["account_tier"]),
            }

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                """
                SELECT id, created_at, characters, confirmed, source, cached, source_lang, target_lang
                FROM usage_events
                ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "created_at": float(r["created_at"]),
                "characters": int(r["characters"]),
                "confirmed": bool(r["confirmed"]),
                "source": str(r["source"]),
                "cached": bool(r["cached"]),
                "source_lang": str(r["source_lang"] or ""),
                "target_lang": str(r["target_lang"] or ""),
            }
            for r in rows
        ]

    def summary(self) -> dict[str, int]:
        with self._lock, self._connect() as db:
            row = db.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN confirmed=0 AND cached=0 THEN characters ELSE 0 END), 0) AS estimated,
                    COALESCE(SUM(CASE WHEN confirmed=1 AND cached=0 THEN characters ELSE 0 END), 0) AS confirmed,
                    COALESCE(SUM(CASE WHEN cached=1 THEN characters ELSE 0 END), 0) AS saved_by_cache,
                    COUNT(*) AS events
                FROM usage_events
                """
            ).fetchone()
            snap = self.get_latest_api_snapshot()
            api_used = int(snap["character_count"]) if snap else 0
            api_limit = int(snap["character_limit"]) if snap else 0
            api_rem = int(snap["character_remaining"]) if snap else 0
            return {
                "estimated_characters": int(row["estimated"]),
                "confirmed_characters": int(row["confirmed"]),
                "saved_by_cache_characters": int(row["saved_by_cache"]),
                "events": int(row["events"]),
                "api_character_count": api_used,
                "api_character_limit": api_limit,
                "api_character_remaining": api_rem,
            }
