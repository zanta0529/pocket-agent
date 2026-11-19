---
title: '開發者指南'
project: 'PocketAgent'
version: '0.1.0'
date: '2025-01-15'
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

```bash
# 1. 克隆專案
git clone https://github.com/zanta0529/pocket-agent.git
cd pocket-agent

# 2. 建立並啟用虛擬環境
python -m venv venv
# Windows (CMD):
venv\Scripts\activate.bat
# Linux / macOS:
source venv/bin/activate

# 3. 升級 pip 並安裝執行期依賴
pip install --upgrade pip
pip install -r requirements.txt

# 4. 安裝開發依賴
pip install -r requirements-dev.txt

# 5. 以可編輯模式安裝
pip install -e .

# 6. (可選) 複製環境變數範本
copy .env.example .env
# 編輯 .env 填入必要設定 (如 API Keys)

# 7. 執行設定精靈 (首次使用)
pocket-agent setup
```

### 1.3 驗證安裝

- **CLI 啟動**：`pocket-agent` (或 `run.bat` / `./run.sh` / `./run.command`)
- **設定精靈**：`pocket-agent setup`
- **圖片生成測試**：`pocket-agent image "a cute robot"`
- **單元測試**：`venv\Scripts\python.exe -m pytest tests/`
- **格式檢查**：`venv\Scripts\python.exe -m ruff format --check .`
- **靜態分析**：`venv\Scripts\python.exe -m ruff check .`
- **匯入完整性**：`venv\Scripts\python.exe scripts\check_imports.py src scripts`

---

## 2. 專案結構

```
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
│   ├── browser_mcp.py          # 瀏覽器自動化 (Playwright)
│   ├── github_mcp.py           # GitHub 整合
│   ├── sqlite_mcp.py           # SQLite 唯讀
│   ├── mysql_mcp.py            # MySQL/MariaDB 唯讀
│   ├── mssql_mcp.py            # SQL Server 唯讀
│   ├── memory_mcp.py           # 跨對話記憶
│   ├── rhel_mcp.py             # RHEL 巡檢 (SSH)
│   ├── win_rdp_mcp.py          # Windows RDP 視覺控制
│   └── _db_common.py           # 資料庫共用基礎設施
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

```
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

1. **定義 Tool Schema** (`tools/<tool>.py`):
   - 遵循 OpenAI Function Calling 格式
   - 標記 `_is_safe_read: True` (唯讀工具) 或預設 False (需確認)

2. **實作呼叫函式** (`call_<tool>_tool`):
   - 參數驗證、錯誤處理、取消檢查 (`check_cancelled`)
   - 回傳字串結果 (供 LLM 繼續)

3. **註冊至 ToolRegistry** (`core/tool_registry.py`):
   - `default_tool_registry.register(TOOLS, call_tool_func)`
   - 敏感工具判定 `is_sensitive()`、取消收尾 `on_cancel()`

4. **新增測試** (`tests/test_<tool>.py`):
   - 正常路徑、邊界條件、錯誤處理、取消行為

### 4.3 新增 MCP Server

1. **建立伺服器腳本** (`mcp_servers/<name>_mcp.py`):
   - 實作 `async def main()`，使用 `mcp.server.Server`
   - 定義工具函式，使用 `@server.call_tool()` 裝飾器
   - 僅提供唯讀/安全操作，遵循 `safe_read_tools` 白名單慣例

2. **更新 MCP Config 範本** (`mcp_config.example.yaml`):
   - 新增 `servers.<name>` 區塊，指定 `transport: stdio`、命令參數
   - 設定 `safe_read_tools` 白名單
   - 定義 `env` 連線參數 (留空以啟用執行期詢問)

3. **更新連線參數規格** (`extensions/mcp.py`):
   - 在 `CONN_PARAM_SPECS` 註冊新參數 (`env_key`, `label`, `default`, `is_secret`)

4. **新增測試** (`tests/test_<name>_mcp.py`):
   - Mock MCP Server、驗證工具呼叫、連線參數注入、錯誤處理

### 4.4 新增 Slash Command

1. **建立 Command 類別** (`frontend/cli/commands/<category>.py`):
   - 實作 `CliCommand` Protocol: `async execute(ctx, args) -> CommandResult`
   - 使用 `CommandContext` 存取 `agent`、`state`、`reader`、`renderer`、`tools`

2. **註冊至 Registry** (`frontend/cli/commands/__init__.py`):
   - 在 `_build_registry()` 中 `registry.register("<name>", CommandClass())`

3. **更新多語系** (`locales/zh_TW.json`, `locales/en_US.json`):
   - 新增指令說明、提示訊息、錯誤訊息鍵值

4. **新增測試** (`tests/test_cli_commands.py`):
   - 測試指令分派、參數解析、互動流程

### 4.5 修改上下文預算參數

- 所有常數集中於 `system/constants.py` (單一權威來源)
- 使用者可透過 `config.json` `system.*` 覆寫
- 修改後須驗證：`test_context_budget.py`、實際大對話場景

---

## 5. 除錯技巧

| 場景         | 方法                                                                    |
| ------------ | ----------------------------------------------------------------------- |
| **本地除錯** | VS Code `launch.json` 設定 `module: "pocket_agent.main"`，啟動偵錯      |
| **CLI 除錯** | `pocket-agent --log-level DEBUG` 或設定 `config.json` `log_level: "DEBUG"` |
| **MCP 除錯** | `python mcp_servers/<name>_mcp.py` 直接執行測試；查看 `mcp_stdio.log`   |
| **日誌分析** | `~/.pocket-agent/workspaces/<slug>/logs/agent.log` (RotatingFileHandler) |
| **效能分析** | `python -m cProfile -o profile.stats -m pocket_agent` → `snakeviz`      |
| **記憶體分析** | `pip install memray` → `memray run -m pocket_agent` → `memray flamegraph` |

---

## 6. 擴充指南

### 6.1 新增模組 (遵循 Adapter/Strategy/Factory/Command)

1. 定義介面/基底類別 (抽象層)
2. 實作具體類別 (封裝差異)
3. 註冊至 Factory/Registry (集中選擇)
4. 前端/呼叫端僅依賴介面 (依賴抽象)

### 6.2 整合外部服務

1. 在 `config.json` / `mcp_config.yaml` 新增設定項
2. 建立 Client/Adapter (`system/providers/` 或 `mcp_servers/`)
3. 實作介面與錯誤處理 (重試、熔斷、逾時)
4. 使用 `constants.py` 管理逾時/緩衝區常數
5. 更新運維手冊 (`docs/architecture/runbook-generated.md`)

---

## 7. 實用腳本

| 腳本           | 用途              | 指令                                    |
| -------------- | ----------------- | --------------------------------------- |
| `run.bat`      | Windows 一鍵啟動  | `run.bat`                               |
| `run.sh`       | Linux 一鍵啟動    | `chmod +x run.sh && ./run.sh`           |
| `run.command`  | macOS 一鍵啟動    | `chmod +x run.command && ./run.command` |
| `build.bat`    | 統一建置 (Portable/PyInstaller) | `build.bat`                    |
| `check_imports.py` | 匯入完整性檢查 | `python scripts/check_imports.py src scripts` |

---

## 8. 參考資源

- **專案 GitHub**：https://github.com/zanta0529/pocket-agent
- **API 文件**：`pocket_agent/` 模組 docstring + 類型提示
- **架構決策記錄 (ADR)**：`docs/architecture/` 6 份架構文件
- **Agensi Skills**：https://agensi.io/skills (Plan-Build-Verify, Reveal.js Presentation)
- **MCP 規範**：https://modelcontextprotocol.io/

---

## 9. 開發流程圖

```mermaid
flowchart TD
    Start[開發開始] --> Branch[建立 feature/fix 分支]
    Branch --> Code[編寫程式碼 + 單元測試]
    Code --> Lint[Ruff format + check]
    Lint --> ImportCheck[check_imports.py]
    ImportTest --> Test[pytest tests/]
    Test --> Commit[提交 (符合 Conventional Commits)]
    Commit --> PR[發起 PR 到 dev/main]
    PR --> CI[GitHub Actions CI]
    CI -->|通過| Review[Code Review]
    CI -->|失敗| FixCI[修復 CI 錯誤]
    FixCI --> CI
    Review -->|核准| Merge[合併]
    Review -->|需修改| Code
    Merge -->|tag v*| Release[Release Workflow 建置 Portable]
    Release --> Done[完成]
```

> **架構師風險評估**：
>
> - [x] 分支策略是否強制 Code Review：PR 合併需通過 CI + 人工審閱
> - [x] CI/CD 門檻是否包含安全掃描：Ruff 靜態分析、import 完整性、pytest；無專門 SAST/DAST 工具
> - [x] 依賴更新是否有自動化檢查：`pip-compile` 鎖定版本、Dependabot 可啟用；`check_imports.py` 防止缺漏
> - [x] 本地開發環境與生產環境一致性：Modern Portable 使用官方 CPython，開發環境 venv 版本一致 (Python 3.14)

---

## 10. 變更記錄

| 版本        | 日期     | 作者       | 變更摘要 |
| ----------- | -------- | ---------- | -------- |
| 0.1.0       | 2025-01-15 | PocketAgent Team | 初版建立 (由 generate-system-docs 自動產生) |