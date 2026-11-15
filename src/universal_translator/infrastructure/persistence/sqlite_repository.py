"""Persistência local em SQLite com migrações automáticas, cache SHA-256, histórico, glossário e perfis."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from ...config.settings_manager import get_app_data_dir
from ...domain.contracts import PersistenceRepositoryPort
from ...domain.entities import (
    ApplicationProfile,
    GlossaryEntry,
    TranslationHistoryEntry,
    TranslationResult,
)
from ...domain.enums import FormalityLevel, ProviderType
from ...domain.services import ProfileMatcher


class SQLiteRepository(PersistenceRepositoryPort):
    """Repositório transacional SQLite thread-safe com controle de versão de schema."""

    SCHEMA_VERSION = 2

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (get_app_data_dir() / "universal_translator.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_database()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_database(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS translation_cache (
                    cache_key TEXT PRIMARY KEY,
                    source_text_hash TEXT NOT NULL,
                    translated_text TEXT NOT NULL,
                    detected_source_lang TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    hit_count INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_cache_expires ON translation_cache(expires_at);

                CREATE TABLE IF NOT EXISTS translation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    translated_text TEXT NOT NULL,
                    source_lang TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    app_name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    cached INTEGER NOT NULL DEFAULT 0,
                    characters_count INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                    latency_ms REAL NOT NULL DEFAULT 0.0,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_history_created ON translation_history(created_at DESC);

                CREATE TABLE IF NOT EXISTS glossary_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_term TEXT NOT NULL,
                    target_term TEXT NOT NULL,
                    source_lang TEXT NOT NULL DEFAULT 'ANY',
                    target_lang TEXT NOT NULL DEFAULT 'ANY',
                    case_sensitive INTEGER NOT NULL DEFAULT 0,
                    do_not_translate INTEGER NOT NULL DEFAULT 0,
                    domain_tag TEXT NOT NULL DEFAULT 'geral',
                    priority INTEGER NOT NULL DEFAULT 10,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    notes TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS app_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_name TEXT NOT NULL UNIQUE,
                    process_names_json TEXT NOT NULL,
                    window_title_patterns_json TEXT NOT NULL,
                    source_lang TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    incoming_target_lang TEXT NOT NULL DEFAULT 'PT-BR',
                    auto_translate_selection INTEGER NOT NULL DEFAULT 0,
                    translate_outgoing INTEGER NOT NULL DEFAULT 1,
                    inline_hotkey TEXT NOT NULL DEFAULT 'ctrl+shift+t',
                    capture_hotkey TEXT NOT NULL DEFAULT 'ctrl+shift+c',
                    formality TEXT NOT NULL DEFAULT 'default',
                    review_before_replace INTEGER NOT NULL DEFAULT 0,
                    floating_button_behavior TEXT NOT NULL DEFAULT 'always_visible',
                    preferred_provider TEXT NOT NULL DEFAULT 'deepl',
                    custom_glossary_domain TEXT NOT NULL DEFAULT 'geral',
                    preserve_context INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                """
            )
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            current_v = row["v"] if row and row["v"] is not None else 0
            if current_v < self.SCHEMA_VERSION:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (self.SCHEMA_VERSION, time.time()),
                )
            conn.commit()

        self._seed_defaults_if_empty()

    def _seed_defaults_if_empty(self) -> None:
        with self._lock, self._connect() as conn:
            g_count = conn.execute("SELECT COUNT(*) AS c FROM glossary_entries").fetchone()["c"]
            if g_count == 0:
                for entry in ProfileMatcher.default_glossary_entries():
                    entry.id = None
                    self.upsert_glossary_entry(entry)

            p_count = conn.execute("SELECT COUNT(*) AS c FROM app_profiles").fetchone()["c"]
            if p_count == 0:
                for profile in ProfileMatcher.default_profiles():
                    profile.id = None
                    self.upsert_profile(profile)

    # --- Cache SHA-256 com TTL ---
    def get_cached_translation(self, cache_key: str) -> TranslationResult | None:
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT translated_text, detected_source_lang, target_lang, provider, expires_at
                FROM translation_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
            if not row:
                return None
            if float(row["expires_at"]) < now:
                conn.execute("DELETE FROM translation_cache WHERE cache_key = ?", (cache_key,))
                conn.commit()
                return None

            conn.execute(
                "UPDATE translation_cache SET hit_count = hit_count + 1 WHERE cache_key = ?",
                (cache_key,),
            )
            conn.commit()
            try:
                provider_enum = ProviderType(row["provider"])
            except ValueError:
                provider_enum = ProviderType.DEEPL

            return TranslationResult(
                translated_text=row["translated_text"],
                source_text="",
                detected_source_lang=row["detected_source_lang"],
                target_lang=row["target_lang"],
                provider=provider_enum,
                cached=True,
                latency_ms=0.5,
                characters_billed=0,
                estimated_cost_usd=0.0,
            )

    def save_cached_translation(
        self, cache_key: str, result: TranslationResult, ttl_seconds: int = 86400 * 30
    ) -> None:
        now = time.time()
        expires = now + max(60, ttl_seconds)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO translation_cache (
                    cache_key, source_text_hash, translated_text,
                    detected_source_lang, target_lang, provider,
                    created_at, expires_at, hit_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    cache_key,
                    cache_key[:32],
                    result.translated_text,
                    result.detected_source_lang,
                    result.target_lang,
                    result.provider.value,
                    now,
                    expires,
                ),
            )
            conn.commit()

    def purge_expired_cache(self) -> int:
        now = time.time()
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM translation_cache WHERE expires_at < ?", (now,))
            conn.commit()
            return cur.rowcount

    # --- Histórico de Traduções ---
    def add_history(self, entry: TranslationHistoryEntry) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO translation_history (
                    operation_id, source_text, translated_text,
                    source_lang, target_lang, provider, app_name,
                    direction, cached, characters_count,
                    estimated_cost_usd, latency_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.operation_id,
                    entry.source_text,
                    entry.translated_text,
                    entry.source_lang,
                    entry.target_lang,
                    entry.provider,
                    entry.app_name,
                    entry.direction,
                    1 if entry.cached else 0,
                    entry.characters_count,
                    entry.estimated_cost_usd,
                    entry.latency_ms,
                    entry.created_at,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def list_history(self, limit: int = 100, search_query: str = "") -> list[TranslationHistoryEntry]:
        query = (search_query or "").strip()
        with self._lock, self._connect() as conn:
            if query:
                like_q = f"%{query}%"
                rows = conn.execute(
                    """
                    SELECT * FROM translation_history
                    WHERE source_text LIKE ? OR translated_text LIKE ? OR app_name LIKE ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (like_q, like_q, like_q, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM translation_history ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()

        return [
            TranslationHistoryEntry(
                id=int(r["id"]),
                operation_id=r["operation_id"],
                source_text=r["source_text"],
                translated_text=r["translated_text"],
                source_lang=r["source_lang"],
                target_lang=r["target_lang"],
                provider=r["provider"],
                app_name=r["app_name"],
                direction=r["direction"],
                cached=bool(r["cached"]),
                characters_count=int(r["characters_count"]),
                estimated_cost_usd=float(r["estimated_cost_usd"]),
                latency_ms=float(r["latency_ms"]),
                created_at=float(r["created_at"]),
            )
            for r in rows
        ]

    def clear_history(self, older_than_days: int | None = None) -> int:
        with self._lock, self._connect() as conn:
            if older_than_days is not None and older_than_days > 0:
                cutoff = time.time() - (older_than_days * 86400)
                cur = conn.execute("DELETE FROM translation_history WHERE created_at < ?", (cutoff,))
            else:
                cur = conn.execute("DELETE FROM translation_history")
            conn.commit()
            return int(cur.rowcount)

    def get_usage_summary(self) -> dict[str, float | int]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_translations,
                    SUM(CASE WHEN cached = 1 THEN 1 ELSE 0 END) AS cached_translations,
                    COALESCE(SUM(characters_count), 0) AS total_characters,
                    COALESCE(SUM(estimated_cost_usd), 0.0) AS total_cost_usd,
                    COALESCE(AVG(latency_ms), 0.0) AS avg_latency_ms
                FROM translation_history
                """
            ).fetchone()
            cache_row = conn.execute("SELECT COUNT(*) AS entries FROM translation_cache").fetchone()
            return {
                "total_translations": int(row["total_translations"] or 0),
                "cached_translations": int(row["cached_translations"] or 0),
                "total_characters": int(row["total_characters"] or 0),
                "total_cost_usd": float(row["total_cost_usd"] or 0.0),
                "avg_latency_ms": float(row["avg_latency_ms"] or 0.0),
                "cache_entries": int(cache_row["entries"] or 0),
            }

    # --- Glossário / Memória Linguística ---
    def list_glossary(self) -> list[GlossaryEntry]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM glossary_entries ORDER BY priority DESC, id ASC"
            ).fetchall()
        return [
            GlossaryEntry(
                id=int(r["id"]),
                source_term=r["source_term"],
                target_term=r["target_term"],
                source_lang=r["source_lang"],
                target_lang=r["target_lang"],
                case_sensitive=bool(r["case_sensitive"]),
                do_not_translate=bool(r["do_not_translate"]),
                domain_tag=r["domain_tag"],
                priority=int(r["priority"]),
                enabled=bool(r["enabled"]),
                notes=r["notes"],
            )
            for r in rows
        ]

    def upsert_glossary_entry(self, entry: GlossaryEntry) -> int:
        with self._lock, self._connect() as conn:
            if entry.id is not None:
                cur = conn.execute(
                    """
                    UPDATE glossary_entries
                    SET source_term=?, target_term=?, source_lang=?, target_lang=?,
                        case_sensitive=?, do_not_translate=?, domain_tag=?,
                        priority=?, enabled=?, notes=?
                    WHERE id=?
                    """,
                    (
                        entry.source_term.strip(),
                        entry.target_term.strip(),
                        entry.source_lang,
                        entry.target_lang,
                        1 if entry.case_sensitive else 0,
                        1 if entry.do_not_translate else 0,
                        entry.domain_tag,
                        entry.priority,
                        1 if entry.enabled else 0,
                        entry.notes,
                        entry.id,
                    ),
                )
                if cur.rowcount > 0:
                    conn.commit()
                    return int(entry.id)

            cur = conn.execute(
                """
                INSERT INTO glossary_entries (
                    source_term, target_term, source_lang, target_lang,
                    case_sensitive, do_not_translate, domain_tag,
                    priority, enabled, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.source_term.strip(),
                    entry.target_term.strip(),
                    entry.source_lang,
                    entry.target_lang,
                    1 if entry.case_sensitive else 0,
                    1 if entry.do_not_translate else 0,
                    entry.domain_tag,
                    entry.priority,
                    1 if entry.enabled else 0,
                    entry.notes,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def delete_glossary_entry(self, entry_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM glossary_entries WHERE id = ?", (entry_id,))
            conn.commit()
            return cur.rowcount > 0

    # --- Perfis Inteligentes por Aplicativo ---
    def list_profiles(self) -> list[ApplicationProfile]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM app_profiles ORDER BY id ASC").fetchall()

        profiles: list[ApplicationProfile] = []
        for r in rows:
            try:
                formality = FormalityLevel(r["formality"])
            except ValueError:
                formality = FormalityLevel.DEFAULT
            try:
                provider = ProviderType(r["preferred_provider"])
            except ValueError:
                provider = ProviderType.DEEPL

            profiles.append(
                ApplicationProfile(
                    id=int(r["id"]),
                    app_name=r["app_name"],
                    process_names=json.loads(r["process_names_json"] or "[]"),
                    window_title_patterns=json.loads(r["window_title_patterns_json"] or "[]"),
                    source_lang=r["source_lang"],
                    target_lang=r["target_lang"],
                    incoming_target_lang=r["incoming_target_lang"],
                    auto_translate_selection=bool(r["auto_translate_selection"]),
                    translate_outgoing=bool(r["translate_outgoing"]),
                    inline_hotkey=r["inline_hotkey"],
                    capture_hotkey=r["capture_hotkey"],
                    formality=formality,
                    review_before_replace=bool(r["review_before_replace"]),
                    floating_button_behavior=r["floating_button_behavior"],
                    preferred_provider=provider,
                    custom_glossary_domain=r["custom_glossary_domain"],
                    preserve_context=bool(r["preserve_context"]),
                    enabled=bool(r["enabled"]),
                )
            )
        return profiles

    def upsert_profile(self, profile: ApplicationProfile) -> int:
        with self._lock, self._connect() as conn:
            proc_json = json.dumps(profile.process_names, ensure_ascii=False)
            title_json = json.dumps(profile.window_title_patterns, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO app_profiles (
                    app_name, process_names_json, window_title_patterns_json,
                    source_lang, target_lang, incoming_target_lang,
                    auto_translate_selection, translate_outgoing,
                    inline_hotkey, capture_hotkey, formality,
                    review_before_replace, floating_button_behavior,
                    preferred_provider, custom_glossary_domain,
                    preserve_context, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(app_name) DO UPDATE SET
                    process_names_json=excluded.process_names_json,
                    window_title_patterns_json=excluded.window_title_patterns_json,
                    source_lang=excluded.source_lang,
                    target_lang=excluded.target_lang,
                    incoming_target_lang=excluded.incoming_target_lang,
                    auto_translate_selection=excluded.auto_translate_selection,
                    translate_outgoing=excluded.translate_outgoing,
                    inline_hotkey=excluded.inline_hotkey,
                    capture_hotkey=excluded.capture_hotkey,
                    formality=excluded.formality,
                    review_before_replace=excluded.review_before_replace,
                    floating_button_behavior=excluded.floating_button_behavior,
                    preferred_provider=excluded.preferred_provider,
                    custom_glossary_domain=excluded.custom_glossary_domain,
                    preserve_context=excluded.preserve_context,
                    enabled=excluded.enabled
                """,
                (
                    profile.app_name.strip(),
                    proc_json,
                    title_json,
                    profile.source_lang,
                    profile.target_lang,
                    profile.incoming_target_lang,
                    1 if profile.auto_translate_selection else 0,
                    1 if profile.translate_outgoing else 0,
                    profile.inline_hotkey,
                    profile.capture_hotkey,
                    profile.formality.value,
                    1 if profile.review_before_replace else 0,
                    profile.floating_button_behavior,
                    profile.preferred_provider.value,
                    profile.custom_glossary_domain,
                    1 if profile.preserve_context else 0,
                    1 if profile.enabled else 0,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM app_profiles WHERE app_name = ?", (profile.app_name.strip(),)
            ).fetchone()
            return int(row["id"]) if row else 0

    def delete_profile(self, profile_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM app_profiles WHERE id = ?", (profile_id,))
            conn.commit()
            return cur.rowcount > 0
