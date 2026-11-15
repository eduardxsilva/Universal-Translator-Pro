"""Serviços de domínio puros: Motor de Glossário, Detector de Idioma e Matcher de Perfis."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .entities import ApplicationProfile, GlossaryEntry, WindowContext
from .enums import FormalityLevel, ProviderType


@dataclass
class GlossaryApplicationResult:
    prepared_text: str
    token_map: dict[str, str]
    applied_terms: list[str]


class GlossaryEngine:
    """Aplica memória linguística personalizada antes e depois da tradução.

    Usa marcadores determinísticos neutros (`__UTGLOSS0__`, `__UTGLOSS1__`) que os provedores
    (DeepL, Google, Microsoft, OpenAI) preservam intactos na estrutura frasal,
    substituindo-os no pós-processamento pelo termo exato definido pelo usuário
    (ex.: "áreas contaminadas" -> "contaminated sites", siglas como "EQuIS", "CETESB").
    """

    def __init__(self, entries: list[GlossaryEntry] | None = None) -> None:
        self._entries: list[GlossaryEntry] = list(entries or [])

    def set_entries(self, entries: list[GlossaryEntry]) -> None:
        self._entries = list(entries)

    def get_active_entries(
        self,
        source_lang: str | None = None,
        target_lang: str | None = None,
        domain_tag: str | None = None,
    ) -> list[GlossaryEntry]:
        active: list[GlossaryEntry] = []
        src_norm = (source_lang or "ANY").upper().split("-")[0]
        tgt_norm = (target_lang or "ANY").upper().split("-")[0]

        for entry in self._entries:
            if not entry.enabled or not entry.source_term.strip():
                continue
            entry_src = (entry.source_lang or "ANY").upper().split("-")[0]
            entry_tgt = (entry.target_lang or "ANY").upper().split("-")[0]

            if entry_src not in ("ANY", "") and src_norm not in ("ANY", "") and entry_src != src_norm:
                continue
            if entry_tgt not in ("ANY", "") and tgt_norm not in ("ANY", "") and entry_tgt != tgt_norm:
                continue
            if domain_tag and domain_tag not in ("geral", "all", "*"):
                if entry.domain_tag not in ("geral", domain_tag):
                    continue
            active.append(entry)

        active.sort(key=lambda e: (e.priority, len(e.source_term)), reverse=True)
        return active

    def pre_process(
        self,
        text: str,
        source_lang: str | None = None,
        target_lang: str | None = None,
        domain_tag: str | None = None,
    ) -> GlossaryApplicationResult:
        if not text or not text.strip():
            return GlossaryApplicationResult(text, {}, [])

        entries = self.get_active_entries(source_lang, target_lang, domain_tag)
        if not entries:
            return GlossaryApplicationResult(text, {}, [])

        working_text = text
        token_map: dict[str, str] = {}
        applied_terms: list[str] = []
        counter = 0

        for entry in entries:
            src = entry.source_term.strip()
            replacement_value = src if entry.do_not_translate else entry.target_term.strip()
            if not replacement_value:
                replacement_value = src

            flags = 0 if entry.case_sensitive else re.IGNORECASE
            prefix = r"\b" if src[0].isalnum() else ""
            suffix = r"\b" if src[-1].isalnum() else ""
            pattern = re.compile(f"{prefix}{re.escape(src)}{suffix}", flags)

            def _replacer(match: re.Match[str], rep_val: str = replacement_value) -> str:
                nonlocal counter
                token = f"__UTGLOSS{counter}__"
                counter += 1
                token_map[token] = rep_val
                applied_terms.append(f"{match.group(0)} → {rep_val}")
                return token

            working_text = pattern.sub(_replacer, working_text)

        return GlossaryApplicationResult(
            prepared_text=working_text,
            token_map=token_map,
            applied_terms=applied_terms,
        )

    @staticmethod
    def post_process(translated_text: str, token_map: dict[str, str]) -> str:
        if not translated_text or not token_map:
            return translated_text
        result = translated_text
        for token, target_value in token_map.items():
            pattern = re.compile(re.escape(token), re.IGNORECASE)
            result = pattern.sub(lambda _, val=target_value: val, result)
        return result


class LanguageDetector:
    """Detector heurístico local ultrarrápido de idioma (zero custo de API e latência <1ms)."""

    _PT_MARKERS = {
        "você", "voce", "não", "nao", "para", "com", "está", "esta", "relatório",
        "relatorio", "amanhã", "amanha", "hoje", "obrigado", "obrigada", "poderia",
        "enviar", "reunião", "reuniao", "também", "tambem", "então", "entao",
        "porque", "quando", "muito", "bom", "boa", "dia", "tarde", "noite",
        "isso", "este", "pelo", "pela", "nosso", "nossa", "fazer", "preciso",
        "revisar", "áreas", "areas", "contaminadas", "favor", "atenciosamente",
        "olá", "ola", "tudo", "bem", "como", "mais", "mas", "dos", "das", "uma",
    }
    _EN_MARKERS = {
        "the", "and", "you", "that", "for", "with", "have", "this", "will", "your",
        "from", "they", "would", "could", "should", "please", "send", "report",
        "tomorrow", "meeting", "thanks", "thank", "hello", "about", "there", "their",
    }
    _ES_MARKERS = {
        "usted", "para", "con", "está", "mañana", "gracias", "podría", "enviar",
        "informe", "reunión", "también", "porque", "cuando", "mucho", "buenos",
        "días", "hola", "todo", "bien", "nosotros", "desde", "hacer", "necesito",
    }
    _FR_MARKERS = {
        "vous", "pour", "avec", "dans", "est", "demain", "merci", "pourriez",
        "envoyer", "rapport", "réunion", "aussi", "parce", "bonjour", "tout",
    }
    _DE_MARKERS = {
        "und", "sie", "das", "ist", "nicht", "mit", "für", "auf", "bitte",
        "morgen", "bericht", "danke", "könnten", "schicken", "hallo",
    }

    @classmethod
    def detect(cls, text: str) -> str:
        if not text or not text.strip():
            return "PT-BR"

        if re.search(r"[\u3040-\u30ff]", text):
            return "JA"
        if re.search(r"[\u4e00-\u9fff]", text):
            return "ZH"
        if re.search(r"[\u0400-\u04ff]", text):
            return "RU"

        words = re.findall(r"[a-záàâãéêíóôõúüçñäöüß]+", text.lower())
        if not words:
            return "PT-BR"

        scores: dict[str, float] = {
            "PT-BR": 0.0,
            "EN-US": 0.0,
            "ES": 0.0,
            "FR": 0.0,
            "DE": 0.0,
        }

        for w in words:
            if w in cls._PT_MARKERS:
                scores["PT-BR"] += 2.0
            if w in cls._EN_MARKERS:
                scores["EN-US"] += 2.0
            if w in cls._ES_MARKERS:
                scores["ES"] += 2.0
            if w in cls._FR_MARKERS:
                scores["FR"] += 2.0
            if w in cls._DE_MARKERS:
                scores["DE"] += 2.0

        lowered = text.lower()
        if any(tok in lowered for tok in ("ã", "õ", "ç", "nh", "lh", "ê", "ô")):
            scores["PT-BR"] += 3.5
        if any(tok in lowered for tok in ("ñ", "¿", "¡")):
            scores["ES"] += 4.0
        if any(tok in lowered for tok in ("ß", "ä", "ö", "ü")):
            scores["DE"] += 2.5

        best_lang, best_score = max(scores.items(), key=lambda item: item[1])
        return best_lang if best_score > 0 else "EN-US"


class ProfileMatcher:
    """Identifica o perfil inteligente adequado para a janela ativa do Windows."""

    def __init__(self, profiles: list[ApplicationProfile] | None = None) -> None:
        self._profiles = list(profiles or self.default_profiles())

    def set_profiles(self, profiles: list[ApplicationProfile]) -> None:
        self._profiles = list(profiles)

    def get_profiles(self) -> list[ApplicationProfile]:
        return list(self._profiles)

    def match(self, context: WindowContext | None) -> ApplicationProfile:
        if context is not None:
            proc = (context.process_name or "").lower()
            title = (context.window_title or "").lower()

            for profile in self._profiles:
                if not profile.enabled:
                    continue
                for pattern in profile.window_title_patterns:
                    if pattern and pattern.lower() in title:
                        return profile

            for profile in self._profiles:
                if not profile.enabled:
                    continue
                for p_name in profile.process_names:
                    if p_name and p_name.lower() == proc:
                        return profile

        return self.global_default_profile()

    @staticmethod
    def global_default_profile() -> ApplicationProfile:
        return ApplicationProfile(
            id=0,
            app_name="Padrão Global",
            process_names=[],
            window_title_patterns=[],
            source_lang="PT-BR",
            target_lang="EN-US",
            incoming_target_lang="PT-BR",
            auto_translate_selection=False,
            translate_outgoing=True,
            inline_hotkey="ctrl+shift+t",
            capture_hotkey="ctrl+shift+c",
            formality=FormalityLevel.DEFAULT,
            review_before_replace=False,
            floating_button_behavior="always_visible",
            preferred_provider=ProviderType.DEEPL,
            custom_glossary_domain="geral",
            preserve_context=True,
            enabled=True,
        )

    @staticmethod
    def default_profiles() -> list[ApplicationProfile]:
        return [
            ApplicationProfile(
                id=1,
                app_name="WhatsApp / WhatsApp Web",
                process_names=["whatsapp.exe"],
                window_title_patterns=["whatsapp"],
                source_lang="PT-BR",
                target_lang="EN-US",
                incoming_target_lang="PT-BR",
                formality=FormalityLevel.INFORMAL,
                review_before_replace=False,
                custom_glossary_domain="chat",
            ),
            ApplicationProfile(
                id=2,
                app_name="Discord",
                process_names=["discord.exe"],
                window_title_patterns=["discord"],
                source_lang="PT-BR",
                target_lang="EN-US",
                incoming_target_lang="PT-BR",
                formality=FormalityLevel.CASUAL,
                review_before_replace=False,
                custom_glossary_domain="chat",
            ),
            ApplicationProfile(
                id=3,
                app_name="Telegram",
                process_names=["telegram.exe"],
                window_title_patterns=["telegram"],
                source_lang="PT-BR",
                target_lang="EN-US",
                incoming_target_lang="PT-BR",
                formality=FormalityLevel.INFORMAL,
                review_before_replace=False,
                custom_glossary_domain="chat",
            ),
            ApplicationProfile(
                id=4,
                app_name="Slack",
                process_names=["slack.exe"],
                window_title_patterns=["slack"],
                source_lang="PT-BR",
                target_lang="EN-US",
                incoming_target_lang="PT-BR",
                formality=FormalityLevel.BUSINESS,
                review_before_replace=False,
                custom_glossary_domain="corporativo",
            ),
            ApplicationProfile(
                id=5,
                app_name="Microsoft Teams",
                process_names=["ms-teams.exe", "teams.exe"],
                window_title_patterns=["microsoft teams", "teams"],
                source_lang="PT-BR",
                target_lang="EN-US",
                incoming_target_lang="PT-BR",
                formality=FormalityLevel.BUSINESS,
                review_before_replace=True,
                custom_glossary_domain="corporativo",
            ),
            ApplicationProfile(
                id=6,
                app_name="Gmail / Webmail",
                process_names=[],
                window_title_patterns=["gmail", "inbox -", "caixa de entrada"],
                source_lang="PT-BR",
                target_lang="EN-US",
                incoming_target_lang="PT-BR",
                formality=FormalityLevel.FORMAL,
                review_before_replace=True,
                custom_glossary_domain="corporativo",
            ),
            ApplicationProfile(
                id=7,
                app_name="Microsoft Outlook",
                process_names=["outlook.exe", "olk.exe"],
                window_title_patterns=["outlook"],
                source_lang="PT-BR",
                target_lang="EN-US",
                incoming_target_lang="PT-BR",
                formality=FormalityLevel.FORMAL,
                review_before_replace=True,
                custom_glossary_domain="corporativo",
            ),
            ApplicationProfile(
                id=8,
                app_name="Navegadores Web",
                process_names=["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"],
                window_title_patterns=[],
                source_lang="PT-BR",
                target_lang="EN-US",
                incoming_target_lang="PT-BR",
                formality=FormalityLevel.DEFAULT,
                review_before_replace=False,
                custom_glossary_domain="geral",
            ),
            ApplicationProfile(
                id=9,
                app_name="Produtividade & Editores (Notepad, Word, VS Code)",
                process_names=["notepad.exe", "winword.exe", "code.exe", "obsidian.exe"],
                window_title_patterns=["bloco de notas", "notepad", "visual studio code"],
                source_lang="PT-BR",
                target_lang="EN-US",
                incoming_target_lang="PT-BR",
                formality=FormalityLevel.DEFAULT,
                review_before_replace=False,
                custom_glossary_domain="tecnico",
            ),
        ]

    @staticmethod
    def default_glossary_entries() -> list[GlossaryEntry]:
        return [
            GlossaryEntry(
                id=1,
                source_term="áreas contaminadas",
                target_term="contaminated sites",
                source_lang="PT",
                target_lang="EN",
                case_sensitive=False,
                do_not_translate=False,
                domain_tag="geral",
                priority=20,
                notes="Terminologia técnica de engenharia ambiental",
            ),
            GlossaryEntry(
                id=2,
                source_term="gerenciamento de áreas contaminadas",
                target_term="contaminated site management",
                source_lang="PT",
                target_lang="EN",
                case_sensitive=False,
                do_not_translate=False,
                domain_tag="geral",
                priority=30,
                notes="Expressão técnica prioritária",
            ),
            GlossaryEntry(
                id=3,
                source_term="EQuIS",
                target_term="EQuIS",
                source_lang="ANY",
                target_lang="ANY",
                case_sensitive=True,
                do_not_translate=True,
                domain_tag="geral",
                priority=50,
                notes="Nome próprio de software (não traduzir)",
            ),
            GlossaryEntry(
                id=4,
                source_term="CETESB",
                target_term="CETESB",
                source_lang="ANY",
                target_lang="ANY",
                case_sensitive=True,
                do_not_translate=True,
                domain_tag="geral",
                priority=50,
                notes="Sigla institucional (não traduzir)",
            ),
        ]
