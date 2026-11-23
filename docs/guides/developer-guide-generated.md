---
title: '開發者指南'
project: 'PocketAgent'
version: '0.1.0'
date: '2025-01-20'
language: 'zh-TW'
---

# PocketAgent 開發者指南

> **語系說明**：本文件語系遵循使用者提問語系（預設繁體中文）。若使用者以英文提問，請切換為英文輸出。

---

## 1. 專案快速上手

### 1.1 環境需求

| 工具          | 版本              | 安裝指令                | 驗證指令               |
| ------------- | ----------------- | ----------------------- | ---------------------- |
| Python        | 3.14+             | 官網下載 / `pyenv install 3.14` | `python --version`      |
| Git           | 2.40+             | `winget install Git.Git` / `brew install git` | `git --version`         |
| pip           | 24.0+             | `python -m ensurepip --upgrade` | `pip --version`         |
| pip-tools     | 7.4+              | `pip install pip-tools` | `pip-compile --version` |
| Ruff          | 0.8+              | `pip install ruff`      | `ruff --version`        |
| pytest        | 8.0+              | `pip install pytest`    | `pytest --version`      |

> **注意**：Windows 開發環境預設使用 `cmd.exe`，PowerShell 受群組原則封鎖。請勿嘗試繞過。

### 1.2 專案克隆與設定

先取得原始碼、進入專案根目錄，確認 `python --version` 為 3.14 以上，再建立虛擬環境。

```text
python -m venv venv
```

Windows（CMD）執行 `venv\Scripts\activate.bat`；Linux／macOS 終端機執行 `source venv/bin/activate`。啟用之後手動執行：

```text
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
python -m pip install -e .
pocket-agent setup
```

第一次設定透過精靈選擇模型供應商與認證方式；MCP 設定可參考 [YAML 範本](../../mcp_config.example.yaml)。不要將實際 API 金鑰、Token 或私人日誌提交至版本庫。來源：[安裝說明](../../README.md)、[套件與 CLI 入口](../../pyproject.toml)、[設定規格](configuration-specification.md)。

### 1.3 驗證安裝

- **CLI 啟動**：`pocket-agent` (或 `run.bat` / `./run.sh` / `./run.command`)
- **設定精靈**：`pocket-agent setup`
- **圖片生成測試**：`pocket-agent image "a cute robot"`
- **單元測試**：`venv\Scripts\python.exe -m pytest tests/`
- **格式檢查**：`venv\Scripts\python.exe -m ruff format --check .`
- **靜態分析**：`venv\Scripts\python.exe -m ruff check .`
- **匯入完整性**：依[專案規範](../PROJECT_INSTRUCTIONS.md#7-驗證方式與手動命令)分別檢查執行期與測試依賴。
- **文件檢查**：修改 `docs/` 請依 [Markdown lint 設定](../../.markdownlint-cli2.jsonc)檢查；[CI](../../.github/workflows/standard-ci.yml)在 Ubuntu 執行 Markdown lint，Python 驗證則在 Windows／Python 3.14 執行。CI 排程不等同本機已通過。

---

## 2. 專案結構

```text
pocket-agent/
├── .agents/                    # 開發代理技能與上下文 (不提交給使用者)
│   ├── context/
│   ├── hooks/
│   └── skills/
├── .github/workflows/          # GitHub Actions CI/CD
│   ├── release.yml             # 發布 workflow (Windows, Python 3.14)
│   └── standard-ci.yml         # 標準 CI (Ruff, pytest, import check)
├── build_scripts/              # 打包腳本
│   ├── build_portable.py       # Modern Portable 打包主腳本
│   ├── pocket.bat              # Portable 啟動器
│   └── PocketAgent.spec        # PyInstaller 規格 (Legacy)
├── dist/                       # 打包產出目錄 (gitignore)
│   └── PocketAgent-portable.zip
├── docs/                       # 專案文件
│   ├── architecture/           # 架構文件 (6 份)
│   ├── guides/                 # 使用指南 (2 份)
│   └── research/               # 研究報告 (3 份)
├── mcp_servers/                # 內建 MCP 伺服器 (Stdio)
│   ├── _shared/                # 跨伺服器共用基礎設施 (bootstrap/redaction/audit/tty/credentials/db_base)
│   ├── sqlite/                 # SQLite 領域子套件 (sql_guard/adapter/server/tools/)
│   ├── mysql/                  # MySQL/MariaDB 領域子套件 (sql_guard/adapter/server/tools/)
│   ├── mssql/                  # SQL Server 領域子套件 (sql_guard/adapter/server/tools/)
│   ├── github/                 # GitHub 領域子套件 (security/client/templates/server/tools/)
│   ├── rhel/                   # RHEL SSH 領域子套件 (security/ssh_client/server/tools/)
│   ├── win_rdp/                # Windows RDP 領域子套件 (security/win32_api/session/server/tools/)
│   ├── sqlite_mcp.py           # SQLite 薄入口
│   ├── mysql_mcp.py            # MySQL/MariaDB 薄入口
│   ├── mssql_mcp.py            # SQL Server 薄入口
│   ├── github_mcp.py           # GitHub 薄入口
│   ├── rhel_mcp.py             # RHEL 巡檢薄入口
│   ├── win_rdp_mcp.py          # Windows RDP 薄入口
│   ├── browser_mcp.py          # 瀏覽器自動化 (Playwright 輕量單檔)
│   └── memory_mcp.py           # 跨對話記憶 (JSON 輕量單檔)
├── scripts/                    # 開發工具腳本
│   ├── check_imports.py        # 匯入完整性檢查
│   └── convert_to_md.py        # 文件轉換
├── src/pocket_agent/           # 核心原始碼
│   ├── core/                   # 核心業務邏輯
│   │   ├── agent.py            # Agent 主迴圈
│   │   ├── tool_registry.py    # 工具派發註冊表
│   │   ├── context_budget.py   # 上下文預算管理
│   │   ├── compaction.py       # 歷史摘要壓縮
│   │   ├── spillover.py        # 落盤機制
│   │   ├── sessions.py         # 會話持久化 (JSON + HMAC)
│   │   ├── subagents.py        # 子代理系統
│   │   ├── cancellation.py     # 非同步取消橋接
│   │   ├── files.py            # 檔案上傳
│   │   ├── activity.py         # 活動追蹤
│   │   ├── instructions.py     # 指令載入
│   │   └── auto_learn.py       # 技能自動萃取
│   ├── engines/                # LLM 引擎 (Adapter Pattern)
│   │   ├── base.py             # BaseEngine 介面 + 事件定義
│   │   ├── factory.py          # EngineFactory
│   │   ├── openai_engine.py    # OpenAI Responses / ChatGPT OAuth
│   │   ├── openrouter_engine.py # OpenRouter
│   │   ├── ollama_engine.py    # Ollama (含 think 狀態機)
│   │   └── chat_engine.py      # Standard Chat Completions (Gemini/Groq/Mistral/SambaNova)
│   ├── extensions/             # 擴充功能
│   │   ├── mcp.py              # MCPManager (核心)
│   │   ├── skills.py           # Skills 載入/遮蔽
│   │   ├── hooks.py            # Hooks 生命週期
│   │   └── plugins.py          # Plugins 外掛封裝
│   ├── frontend/cli/           # CLI 前端
│   │   ├── main.py             # CliFrontend 進入點
│   │   ├── input.py            # CliInput (prompt-toolkit)
│   │   ├── render.py           # CliRenderer (Rich)
│   │   ├── state.py            # CliState
│   │   ├── confirmation.py     # ConfirmationBridge
│   │   ├── interrupts.py       # 中斷處理
│   │   ├── clipboard.py        # 智慧剪貼簿
│   │   └── commands/           # Slash Commands (Command Pattern)
│   │       ├── core.py         # 基礎類別
│   │       ├── misc.py         # 雜項指令
│   │       ├── session.py      # Session 管理
│   │       ├── subagents.py    # Subagent 指令
│   │       └── system.py       # 系統指令
│   ├── tools/                  # 內建工具
│   │   ├── fs.py               # 原生檔案操作
│   │   ├── shell.py            # Shell 執行
│   │   ├── web.py              # 網路請求/爬蟲
│   │   ├── image.py            # 圖片生成
│   │   ├── doc.py              # 文件產出 (docx/pdf/pptx)
│   │   ├── document.py         # 文件讀取 (pdf/docx/pptx)
│   │   ├── rag.py              # FTS5 檢索
│   │   └── repo_map.py         # AST 結構圖
│   ├── system/                 # 系統基礎設施
│   │   ├── config.py           # 階層式配置 (Strategy Pattern)
│   │   ├── auth.py             # OAuth PKCE + Keyring/JWE
│   │   ├── paths.py            # 路徑解析
│   │   ├── constants.py        # 常數集中管理
│   │   ├── providers/          # 供應商策略
│   │   ├── permissions.py      # 指令白名單
│   │   ├── registry.py         # Markdown 註冊表
│   │   ├── commands.py         # 巨集指令
│   │   ├── sys_commands.py     # 系統指令
│   │   ├── wizard.py           # 設定精靈
│   │   ├── housekeeping.py     # 自動管家
│   │   ├── updater.py          # 線上更新
│   │   ├── compat.py           # 相容處理
│   │   └── i18n.py             # 多語系
│   └── locales/                # 語系檔
│       ├── en_US.json
│       └── zh_TW.json
├── subagents/                  # 內建子代理人設
│   └── researcher.md
├── tests/                      # 測試碼 (pytest)
│   ├── conftest.py             # 全域 fixture (隔離 user_dir, 固定 zh_TW)
│   ├── test_*.py               # 100+ 測試檔
│   └── __pycache__/
├── .editorconfig               # 編輯器設定
├── .gitignore
├── .markdownlint-cli2.jsonc    # Markdown lint 設定
├── .pre-commit-config.yaml     # Pre-commit hooks
├── .prettierrc                 # Prettier 設定
├── AGENTS.md                   # 專案指引入口
├── build.bat                   # 統一建置腳本
├── config-schema.json          # 設定 JSON Schema
├── LICENSE
├── mcp_config.example.yaml     # MCP 設定範本
├── mcp_config.yaml             # MCP 設定 (gitignore)
├── pyproject.toml              # 專案元資料 + Ruff/pytest 設定
├── README.md                   # 英文說明
├── README.zh-TW.md             # 繁體中文說明
├── requirements-dev.in         # 開發依賴來源
├── requirements-dev.txt        # 開發依賴鎖定 (pip-compile 產出)
├── requirements.in             # 執行期依賴來源
├── requirements.txt            # 執行期依賴鎖定 (pip-compile 產出)
├── run.bat                     # Windows 啟動器
├── run.command                 # macOS 啟動器
└── run.sh                      # Linux 啟動器
```

---

## 3. 開發工作流

### 3.1 分支策略

| 分支類型     | 命名規則                                   | 用途         | 合併目標           |
| ------------ | ------------------------------------------ | ------------ | ------------------ |
| **主分支**   | `main`                                     | 生產環境就緒 | -                  |
| **開發分支** | `dev`                                      | 整合測試     | `main`             |
| **功能分支** | `feature/<描述>`                           | 新功能開發   | `dev`              |
| **修復分支** | `fix/<描述>`                               | 錯誤修復     | `dev` / `main`     |
| **發布分支** | `release/v<版本>`                          | 發布準備     | `main` + `dev`     |
| **熱修復**   | `hotfix/<描述>`                            | 線上緊急修復 | `main` + `dev`     |

> 發布流程：推送 `v*` tag 觸發 `.github/workflows/release.yml` 自動建置 Modern Portable 並建立 GitHub Release。

### 3.2 提交訊息規範

```text
<type>(<scope>): <subject>

<body>

<footer>
```

| Type       | 說明                  |
| ---------- | --------------------- |
| `feat`     | 新功能                |
| `fix`      | 錯誤修復              |
| `docs`     | 文件更新              |
| `style`    | 格式調整 (不影響邏輯) |
| `refactor` | 重構                  |
| `perf`     | 效能優化              |
| `test`     | 測試相關              |
| `chore`    | 建置/工具/依賴更新    |

### 3.3 程式碼品質門檻

| 檢查項目     | 工具                 | 門檻                               | 執行指令                          |
| ------------ | -------------------- | ---------------------------------- | --------------------------------- |
| **靜態分析** | Ruff                 | 0 errors                           | `ruff check .`                    |
| **格式化**   | Ruff                 | 通過                               | `ruff format --check .`           |
| **單元測試** | pytest               | 全數通過                           | `pytest tests/`                   |
| **匯入完整性** | check_imports.py   | 無未宣告匯入、無缺少依賴            | `python scripts/check_imports.py src scripts` |
| **Markdown Lint** | markdownlint-cli2 | 0 errors                          | `markdownlint-cli2 docs/`         |

> **重要**：`requirements.txt` 和 `requirements-dev.txt` 為 `pip-compile` 產出的鎖定檔，**嚴禁手動修改**。修改依賴請編輯 `.in` 檔案後重新編譯。

---

## 4. 常見開發任務

### 4.1 新增 LLM Provider / Engine

1. **新增 Provider 策略** (`src/pocket_agent/system/providers/`):
   - 繼承 `OpenAICompatibleProvider` (`base.py`)
   - 實作 `get_client()`、`fetch_models()`
   - 在 `__init__.py` 註冊

2. **新增 Engine** (若協定特殊):
   - 繼承 `BaseEngine` (`engines/base.py`)
   - 實作 `stream_chat()` 產出統一事件 (`EventText`, `EventThinking`, `EventToolCallStart`, `EventUsage`)
   - 在 `EngineFactory.ENGINE_MAP` 註冊 (`engines/factory.py`)

3. **更新常數** (`system/constants.py`):
   - 新增 `PROVIDER_<NAME>` 識別字串

4. **更新設定精靈** (`system/wizard.py`):
   - 新增提供者選項、OAuth/金鑰輸入流程

5. **新增測試** (`tests/test_<provider>_engine.py`):
   - 模擬串流回應、工具呼叫、錯誤處理

### 4.2 新增內建工具

1. 在 `src/pocket_agent/tools/` 建立新模組 (如 `new_tool.py`)
2. 定義 `NEW_TOOLS: list[dict]` (OpenAI Responses 格式) 並實作 `call_new_tool()`
3. 在 `core/tool_registry.py` 的 `default_tool_registry` 註冊
4. 實作 `is_sensitive` 判定、`on_cancel` 收尾邏輯
5. 新增測試 `tests/test_new_tool.py` (隔離網路/檔案系統)

### 4.3 新增 MCP Server

1. 在 `mcp_servers/` 建立 `<name>_mcp.py`
2. 實作 `main()` 入口：`stdio_server` 為主傳輸層
3. 使用 `_db_common.py` 共用基礎設施 (資料庫連線池、健康檢查)
4. 在 `mcp_config.example.yaml` 新增 server 範本 (`command`, `args`, `env`, `inject`, `safe_read_tools`)
5. **安全**：唯讀查詢、受控健康診斷、SSH/RDP 雙軌驗證、密碼不落盤
6. 新增測試 `tests/test_<name>_mcp.py`

### 4.4 新增 CLI 指令

1. 在 `frontend/cli/commands/` 建立新模組 (如 `new_command.py`)
2. 實作 `CliCommand` Protocol: `async execute(ctx, args) -> CommandResult`
3. 在 `CliCommandRegistry` (或 `_build_registry`) 註冊
4. 支援多語系：`pocket_agent.system.i18n.t(key)`，更新 `locales/*.json`
5. 新增測試 `tests/test_cli_commands.py`

### 4.5 新增子代理

1. 在 `subagents/` 建立 `<name>.md` (遵循 `researcher.md` 格式)
2. 在 `core/subagents.py` 的 `SUBAGENTS` 註冊
3. 定義系統提示詞、可用工具清單、任務範疇

---

## 5. 核心開發規範

### 5.1 架構設計原則 (Adapter/Strategy/Factory/Command)

- **Adapter**：引擎層轉換各家 SDK 為統一 `stream_chat` 契約；工具層統一透過 `ToolRegistry` 擴充
- **Strategy**：新增 OpenAI 相容供應商優先繼承 `OpenAICompatibleProvider`；前端查詢能力旗標而非硬編碼 provider
- **Factory**：`EngineFactory` 集中選擇及建立引擎；呼叫端顯式傳入 `provider`
- **Command**：斜線指令透過 `CliCommandRegistry` 註冊獨立實作，禁止巨型 `if/elif` 路由器

### 5.2 編碼規範

- Python 4 空白縮排、UTF-8、LF、檔尾換行；行長 ≤ 120 字元
- **Ruff E501 防範**：超過 120 字元使用隱式字串拼接拆分；含 CJK 字元預留安全餘裕
- **常數集中管理**：所有魔術數字維護於 `system/constants.py`
- 格式以 `pyproject.toml` Ruff 設定為準：雙引號、E/F/I 規則
- 新增程式以 Python 3.14 相容為基準，補型別與錯誤處理

### 5.3 多語系 (i18n) 開發規範

- 所有使用者可見文字維護於 `src/pocket_agent/locales/*.json` (`zh_TW.json`, `en_US.json`)
- 透過 `pocket_agent.system.i18n.t(key, **kwargs)` 讀取，**嚴禁寫死顯示文字**
- 新增/調整文字時，**務必同步更新所有語系檔**
- 不可翻譯：內部工具名稱、System Prompt、內部前綴比對邏輯、後台 logging
- 獨立 MCP 伺服器：`try/except ImportError` 提供降級函式

### 5.4 上下文與草稿區

- `ContextBudget` 動態預算管理：`有效視窗 − 預留輸出 − 安全餘裕`
- 送出前統一流程：清除舊工具輸出 (60%) → `HistoryCompactor` 壓縮 → 安全滑動視窗 → 強制停止
- 使用者可微調：`system.context_window`、`system.context_reserve_output`、`system.context_safety_margin`、`system.context_compact_target_ratio`
- `read_file` 具 500 行與 12,000 字元雙重限制，支援 `char_offset`
- 大型任務**必須**寫入暫存區 (`~/.pocket-agent/workspaces/<slug>/scratch/notes.md`)，**嚴禁**在工作目錄建立 `.agents/scratch`

### 5.5 安全不變量

- **確認回呼契約 (`on_confirm`)**：檔案寫入/敏感操作僅在 `decision is True` 時放行；非 `True` 視為拒絕
- **指令白名單 (`permissions.py`)**：保留 `strip()` 後參數與引號邊界，`shlex` 分詞失敗 fail-closed
- **唯讀資料庫/RDP 隔離**：強制唯讀查詢、零鎖定巡檢、容量熔斷；RDP 綁定受信 `mstsc.exe`
- **引擎工具集邊界**：Chat Completions 僅在 `finish_reason == "tool_calls"` 派發工具
- **原生工具優先**：檔案操作必須用內建工具，**嚴禁** shell 包裝 `echo`/`sed`/`grep` 等
- **環境限制**：Windows 僅 `cmd.exe`，PowerShell 受封鎖
- **操作審查**：不自動執行測試/Lint/建置/安裝/Git 提交；由使用者手動執行

---

## 6. 測試指南

### 6.1 測試架構

- **框架**：pytest + `conftest.py` 全域 fixture
- **隔離**：`tmp_path` 隔離 `user_dir`、固定 `zh_TW` 語系、清除 `POCKET_AGENT_LANG`
- **模式**：非同步測試使用 `pytest-asyncio`、`AsyncMock` 模擬串流

### 6.2 關鍵測試檔案

| 測試檔案 | 覆蓋範圍 |
|---------|---------|
| `test_agent_resilience.py` | Agent 韌性、工具派發、取消、中斷 |
| `test_context_budget.py` | 預算計算、壓縮觸發、Spillover |
| `test_cli_flow.py` | CLI 完整互動流程 |
| `test_mcp_manager.py` | MCP 連線、工具命名空間、Secret Inject |
| `test_security_agent.py` | 安全邊界、確認回呼、白名單 |
| `test_*_mcp.py` | 各 MCP Server 功能與安全 |

### 6.3 執行測試

```cmd
# 全量測試
venv\Scripts\python.exe -m pytest tests/

# 特定模組
venv\Scripts\python.exe -m pytest tests/test_cli_flow.py

# 關鍵字過濾
venv\Scripts\python.exe -m pytest -k "mcp" tests/
```

---

## 7. 打包與發布

### 7.1 Modern Portable (推薦)

```cmd
# 建置
build.bat

# 或手動
venv\Scripts\python.exe build_scripts\build_portable.py
```

產出：`dist/PocketAgent-portable.zip` (含 bundled Python、官方簽章 `pythonw.exe`)

### 7.2 Legacy PyInstaller (不推薦)

```cmd
build.bat
# 選擇 PyInstaller 選項
```

風險：防毒誤判率高、啟動較慢、不支援官方簽章

### 7.3 CI/CD 發布流程

1. 推送 `v*` tag (如 `v0.1.0`)
2. `.github/workflows/release.yml` 自動觸發
3. Windows-latest + Python 3.14 執行建置
4. 上傳 `PocketAgent-portable.zip` 至 GitHub Release

---

## 8. 參考資源

- **專案指引**：`AGENTS.md`、`docs/PROJECT_INSTRUCTIONS.md`
- **架構文件**：`docs/architecture/*.md` (6 份)
- **現有文件**：`README.md`、`README.zh-TW.md`
- **MCP 開發**：`mcp_servers/README.md`
- **GitHub Actions**：`.github/workflows/*.yml`

---

## 9. 變更記錄

| 版本        | 日期     | 作者       | 變更摘要 |
| ----------- | -------- | ---------- | -------- |
| 0.1.0       | 2025-01-20 | PocketAgent Team | 初版建立 (由 generate-system-docs 自動生成) |
