# Universal Translator Pro (v3.0.0)

Tradutor universal residente para Windows Desktop, projetado em **Clean Architecture** para eliminar a necessidade de copiar textos, abrir tradutores externos ou alternar janelas. Funciona como uma camada inteligente integrada à digitação natural do usuário, combinando um **Botão Flutuante Minimalista** com **Monitoramento Automático de 5 Segundos** e integração exclusiva com a **API REST Oficial da DeepL (`/v2/translate` e `/v2/usage`)**.

---

## 1. Visão e Fluxo de Funcionamento

1. O usuário ativa o **Modo de Tradução Automática** clicando no centro do botão flutuante discreto.
2. Escreve normalmente em qualquer aplicativo compatível no Windows (WhatsApp Web/Desktop, Discord, Telegram, Slack, Microsoft Teams, Gmail, Outlook, Editores e Navegadores).
3. Cada interação relevante de edição (digitação, `Backspace`, `Delete`, seleção, colagem) reinicia o temporizador inteligente de **5 segundos**.
4. Após 5 segundos de inatividade:
   - O sistema verifica se o controle ativo é um campo editável válido e não sensível;
   - Captura o conteúdo integral do campo (priorizando **Windows UI Automation** / mensagens nativas Win32 e utilizando área de transferência controlada com restauração integral apenas como fallback);
   - Consulta o **Cache Persistente SQLite (SHA-256)** e deduplica requisições simultâneas;
   - Quando não há tradução em cache, envia o texto integral para a **DeepL REST API (`/v2/translate`)** preservando contexto, formalidade, parágrafos, quebras de linha, pontuação e emojis;
   - Valida novamente se a janela, o campo focado e o conteúdo original permanecem inalterados;
   - Substitui o texto original pela tradução preservando o cursor ao final da mensagem.

---

## 2. Estrutura do Botão Flutuante (3 Áreas Interativas)

- **Centro (`Botão Principal`)**: Ativa e desativa imediatamente o modo de tradução automática, exibindo visualmente o estado atual (`Inativo`, `Monitorando`, `Traduzindo`, `Concluído`, `Alerta/Erro`).
- **Canto Superior Esquerdo (`Ícone de Idiomas`)**: Abre o painel rápido com **Detecção Automática**, **Idioma de Origem**, **Idioma de Destino**, tradução sob demanda e atalho direto para a aba **Configurações de Idiomas**.
- **Canto Superior Direito (`Ícone ×`)**: Fecha/oculta o botão flutuante e **interrompe imediatamente o monitoramento automático**, impedindo novas traduções até que o recurso seja reativado.
- **Zero Roubo de Foco**: Implementado com estilos nativos Win32 (`WS_EX_NOACTIVATE` + interceptação `WM_MOUSEACTIVATE -> MA_NOACTIVATE`) e atributos Qt (`WA_ShowWithoutActivating`), garantindo que clicar no botão flutuante jamais retire o foco do campo de mensagem ativo.

---

## 3. Painel Principal (6 Seções)

1. **1. Visão Geral**: Estado do tradutor, idiomas configurados, status da conexão com a DeepL, última tradução realizada e status operacional.
2. **2. Idiomas**: Idioma de origem, idioma de destino, detecção automática, formalidade e configurações avançadas de Glossário Técnico / Termos Protegidos.
3. **3. DeepL**: Campo de API Key (armazenada exclusivamente no **Windows Credential Manager**), seleção de conta `Free` / `Pro` / `Automático`, botões **Testar Conexão** e **Consultar Utilização (`/v2/usage`)** e status de autenticação.
4. **4. Traduções**: Visualização detalhada de texto original e texto traduzido, busca e tabela de histórico com data/hora e indicação explícita de tradução via **CACHE** ou **API**.
5. **5. Consumo**: Caracteres utilizados confirmados pela API DeepL, limite informado, saldo disponível (sem presumir renovação mensal), separação entre valores confirmados e estimativas locais, economia gerada pelo cache SQLite, histórico de eventos e alertas configuráveis de utilização (`%`).
6. **6. Automação**: Ativação/desativação da tradução automática, intervalo de inatividade configurável (padrão 5s), configurações de captura (UI Automation / validação pré-colagem), inicialização configurável com o Windows (`HKCU\...\Run`), perfis de aplicativos compatíveis, controles de privacidade e logs sanitizados.

---

## 4. Proteção contra Interferências e Limitações Técnicas Conhecidas

- **Proteções ativas**:
  - Nunca traduz a mesma mensagem repetidamente nem retraduz o próprio resultado inserido (`FieldTranslationGuardState` por `hwnd` + `focused_control_id`).
  - Cancela imediatamente a operação se o usuário mudar de janela, trocar de campo ou voltar a digitar durante a captura/tradução.
  - Bloqueia campos de senha (`ES_PASSWORD`, `UIA IsPassword`) e janelas de cofres de credenciais/autenticação.
  - Nunca apaga ou substitui o texto original em caso de falha HTTP (`401`, `403`, `429`, `456`, `5xx`) ou indisponibilidade de rede.
- **Limitações técnicas documentadas**:
  - Superfícies renderizadas puramente via DirectX/OpenGL/Canvas sem exposição de árvore UI Automation e sem suporte a comandos padrão de edição são classificadas como `requires_ocr_or_unaccessible` e ignoradas por segurança.
  - Processos executados como Administrador (elevados via UAC) exigem que o tradutor também seja executado com o mesmo nível de privilégio (UIPI do Windows) para receber eventos sintéticos.

---

## 5. Instalação, Testes e Empacotamento Windows (`.exe`)

### Execução em Desenvolvimento (Windows PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

### Execução da Suite de Testes Automatizados

```powershell
.\scripts\run_tests.ps1
# Ou diretamente:
$env:PYTHONPATH = "src;."
python -m unittest discover -s tests -p "test_*.py" -v
```

### Geração do Executável Standalone (`UniversalTranslatorPro.exe`)

```powershell
.\scripts\build_windows_exe.ps1
# Ou dê duplo-clique em GERAR_EXECUTAVEL.bat
```
