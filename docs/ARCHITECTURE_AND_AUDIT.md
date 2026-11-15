# Arquitetura e Auditoria Técnica — Universal Translator Pro (v3.0.0)

## 1. Visão Arquitetural (Clean Architecture & SOLID)

```text
universal_translator/
├── src/
│   └── universal_translator/
│       ├── domain/                  # Entidades, Enums (ControlAccessibilityKind, DeepLAccountType), Contratos (Ports) e Serviços Puros
│       ├── application/
│       │   ├── orchestrator.py      # TranslationOrchestrator (Integração exclusiva DeepL, Deduplicação concorrente, Cache SHA-256)
│       │   └── auto_monitor.py      # AutoTranslationMonitor (Temporizador inteligente de 5s, Elegibilidade, Anti-loop, Anti-interferência)
│       ├── infrastructure/
│       │   ├── providers/           # DeepLProvider (/v2/translate e /v2/usage, HTTP 401/403/429/456, Retry-After + Backoff/Jitter)
│       │   ├── windows/             # Win32WindowInspector (HWND, Focus Child, ES_PASSWORD, WS_EX_NOACTIVATE) + WindowsStartupManager
│       │   ├── accessibility/       # UIAutomationAdapter (Leitura/Escrita integral via UIA ValuePattern/TextPattern e Win32 EM_GETSEL/EM_SETSEL)
│       │   ├── clipboard/           # SafeTextCaptureEngine (capture_entire_field e replace_entire_field_safely com restauração e validação)
│       │   ├── persistence/         # SQLiteRepository (Cache SHA-256 com TTL, Histórico, Glossário, Perfis) + UsageTracker (Snapshots e Eventos)
│       │   ├── security/            # CredentialManager (Windows Credential Manager Advapi32 + Fallback PBKDF2-SHA256)
│       │   └── logging/             # ObservabilityLogger + PrivacyRedactingFilter (Zero textos privados ou chaves nos logs)
│       ├── presentation/
│       │   ├── windows/             # MainWindow (6 Seções: Visão Geral, Idiomas, DeepL, Traduções, Consumo, Automação)
│       │   ├── widgets/             # FloatingButton (3 Áreas Interativas sem roubo de foco) + SimpleFloatingActionPanel
│       │   └── styles/              # Tema Dark Minimalista Moderno (QSS)
│       ├── services/                # GlobalHotkeyService
│       ├── config/                  # AppSettings & SettingsManager ( Persistência atômica sem credenciais em texto puro)
│       └── main.py                  # Ponto de entrada compatível com PyInstaller (_MEIPASS)
├── tests/
│   ├── unit/                        # test_deepl_professional.py, test_domain_and_security.py
│   ├── integration/                 # test_providers_cache_and_sqlite.py
│   └── ui/                          # test_capture_replace_and_regressions.py
├── scripts/                         # run_tests.ps1, build_windows_exe.ps1
├── UniversalTranslatorPro.spec      # Especificação PyInstaller --onefile sem console
└── GERAR_EXECUTAVEL.bat             # Compilador automatizado Windows
```

## 2. Matriz de Conformidade com a Especificação Global

| Requisito da Especificação | Componente Responsável | Implementação e Verificação |
| :--- | :--- | :--- |
| **Botão Flutuante (3 Áreas Interativas)** | `FloatingButton` / `SimpleFloatingActionPanel` | Centro ativa/desativa modo automático; canto sup. esquerdo abre painel com origem, destino, detecção automática e link para configurações; canto sup. direito (`×`) fecha e interrompe o monitoramento. |
| **Temporizador Inteligente de 5s** | `AutoTranslationMonitor` + `MainWindow` | Reinicia a cada tecla de edição, `Backspace`, `Delete`, seleção ou colagem; ignora movimento puro de mouse; cancela ao trocar de janela ou campo. |
| **Captura Integral & Acessibilidade** | `UIAutomationAdapter` + `SafeTextCaptureEngine` | Prioriza `WM_GETTEXT` e UI Automation (`ValuePattern`/`TextPattern`) antes do fallback `Ctrl+A`/`Ctrl+C`, preservando clipboard e cursor. |
| **Proteção contra Interferências** | `AutoTranslationMonitor` + `Win32WindowInspector` | Impede retradução da mesma mensagem ou do próprio resultado (`FieldTranslationGuardState`), bloqueia senhas (`ES_PASSWORD` / `IsPassword`) e valida hash antes de colar. |
| **Integração Exclusiva DeepL** | `DeepLProvider` + `TranslationOrchestrator` | Usa `/v2/translate` e `/v2/usage`, suporta contas Free (`:fx`) e Pro, trata HTTP 401, 403, 429 (`Retry-After`), 456 e falhas de rede sem fallback silencioso. |
| **Economia de Caracteres** | `SQLiteRepository` + `UsageTracker` | Cache persistente SQLite, deduplicação de chamadas simultâneas e separação entre saldo da API, caracteres confirmados, estimados e economizados pelo cache. |
| **Privacidade e Segurança** | `CredentialManager` + `PrivacyRedactingFilter` | Chave armazenada no Windows Credential Manager (`Advapi32.dll`), HTTPS obrigatório e sanitização automática de logs. |
