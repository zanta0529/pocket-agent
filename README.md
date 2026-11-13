# PocketAgent 🤖

[![Python Version](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code style: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](#)

## 📖 專案簡介 (Introduction)

**PocketAgent** 是一款具備企業級擴充性與安全性的本機端 Python AI 代理工具，提供極簡、高效且專注於開發者體驗的命令列 (CLI) 介面。

本專案的核心優勢在於採用了 **OAuth PKCE 認證直接串接 ChatGPT 後端 (Codex API)**，這意味著只要您擁有 ChatGPT Plus/Pro 帳號，即可免費運用進階模型與深度的邏輯推理 (Reasoning) 能力，完全不需要申請或消耗 OpenAI API Key 的額外計費餘額。同時，專案也完美相容於 **OpenRouter API**、**Gemini (Google AI Studio)**（支援 API Key 與 Google OAuth 雙軌登入），並廣泛支援各大具備免費開發者額度的雲端推理平台（包含 **Groq Cloud**、**SambaNova Cloud** 與 **Mistral AI**）。此外，內建強大的本地端大語言模型 (Ollama) 與 **Ollama Cloud** API Key 串接支援，滿足對資料隱私具高度要求的離線開發及雲端混合場景。

> ⚠️ **重要免責提醒（Important Disclaimer）**：本工具（含核心代理與所有內建／外掛 MCP 伺服器）具備執行本機指令、讀寫檔案及操作遠端主機與瀏覽器之能力，且 AI 模型輸出具非確定性。使用者須自行審查操作並承擔一切風險，任何非預期行為或衍生損害皆與原作者無關。使用前請務必詳閱文末完整的「免責聲明（Disclaimer）」章節。

### 📑 目錄 (Table of Contents)

- [🚀 初次安裝與快速啟動 (Installation & First Run)](#-初次安裝與快速啟動-installation--first-run)
  - [途徑 A：Windows 免安裝可攜版 (Modern Portable) [推薦一般使用者]](#途徑-awindows-免安裝可攜版-modern-portable-推薦一般使用者)
  - [途徑 B：跨平台一鍵啟動腳本 [推薦快速體驗]](#途徑-b跨平台一鍵啟動腳本-推薦快速體驗)
  - [途徑 C：開發者手動安裝 [推薦二次開發]](#途徑-c開發者手動安裝-推薦二次開發)
  - [首次設定精靈 (First-Time Setup)](#首次設定精靈-first-time-setup)
- [💻 快速上手與日常操作 (Quick Start & Usage)](#-快速上手與日常操作-quick-start--usage)
  - [終端互動快捷鍵](#終端互動快捷鍵)
  - [核心斜線指令 (Slash Commands)](#核心斜線指令-slash-commands)
- [✨ 核心架構與特色 (Architecture & Features)](#-核心架構與特色-architecture--features)
  - [1. 雙軌大模型轉接架構](#1-雙軌大模型轉接架構-llm-engine-adapter-architecture)
  - [2. 本地文件產生與匯出](#2-本地文件產生與匯出-native-document-generation)
  - [3. 多重代理與擴充性](#3-多重代理與擴充性-extensibility--multi-agent)
  - [4. 企業級安全治理與工作區零污染](#4-企業級安全治理與工作區零污染-enterprise-governance)
  - [5. 深入架構與實作文件](#5-深入架構與實作文件-deep-dive-documentation)
- [⚙️ 進階設定與客製化 (Configuration & Customization)](#️-進階設定與客製化-configuration--customization)
  - [全域系統設定 (`config.json`)](#全域系統設定-configjson)
  - [分層配置與工作區規範 (`AGENTS.md`)](#分層配置與工作區規範-agentsmd)
  - [外部 MCP 伺服器掛載與注入](#外部-mcp-伺服器掛載與注入)
- [🛠️ 開發與貢獻 (Development & Contributing)](#️-開發與貢獻-development--contributing)
- [📦 發布與建置 (Build & Deployment)](#-發布與建置-build--deployment)
- [⚠️ 免責聲明 (Disclaimer)](#️-免責聲明-disclaimer)
- [📜 授權條款 (License)](#-授權條款-license)

---

## 🚀 初次安裝與快速啟動 (Installation & First Run)

### 系統環境需求

- **Python 3.14 或以上版本**（若選擇「途徑 A：可攜版」則本機無需安裝 Python）。
- 支援作業系統：Windows 10/11、macOS (Apple Silicon / Intel)、Linux (Ubuntu, Debian, RHEL 等)。

---

### 途徑 A：Windows 免安裝可攜版 (Modern Portable) [推薦一般使用者]

如果您是在 Windows 環境，或身處無法隨意安裝全域 Python／受嚴格防毒政策控管的企業環境：

1. 前往本專案的 **GitHub Releases** 頁面，下載最新的 `PocketAgent-portable.zip`。
2. 解壓縮至任意資料夾（例如 `D:\PocketAgent`）。
3. 進入解壓縮目錄，雙擊執行 **`pocket.bat`** 即可立即啟動！

> 💡 **可攜版特色**：隨附官方簽章 Python 獨立環境與完整相依套件，不會污染您電腦既有的 Python 環境，且完美避開防毒軟體對自編譯 `.exe` 的誤判。後續可直接在 CLI 輸入 `/update` 線上追蹤最新更新。

---

### 途徑 B：跨平台一鍵啟動腳本 [推薦快速體驗]

專案原始碼內建各作業系統的自動化引導腳本。腳本會自動偵測系統中的 Python 執行檔、建立獨立虛擬環境 (`venv`)、安裝相依套件並啟動代理：

```bash
# 1. 複製儲存庫
git clone https://github.com/zanta0529/pocket-agent.git
cd pocket-agent
```

依您的作業系統執行對應啟動腳本：

- **🪟 Windows (命令提示字元 / CMD)**：
  ```cmd
  run.bat
  ```
- **🐧 Linux (終端機)**：
  ```bash
  chmod +x run.sh
  ./run.sh
  ```
- **🍎 macOS (終端機或 Finder 雙擊)**：
  ```bash
  chmod +x run.command
  ./run.command
  ```

---

### 途徑 C：開發者手動安裝 [推薦二次開發]

若您希望手動掌控虛擬環境，或將 `pocket-agent` 註冊為終端機全域命令：

```bash
# 1. 複製儲存庫並進入目錄
git clone https://github.com/zanta0529/pocket-agent.git
cd pocket-agent

# 2. 建立並啟用 Python 3.14+ 虛擬環境
python -m venv venv
# Linux / macOS:
source venv/bin/activate
# Windows (CMD):
venv\Scripts\activate.bat

# 3. 升級 pip 並安裝執行期相依套件
pip install --upgrade pip
pip install -r requirements.txt

# 4. 以可編輯模式 (Editable Mode) 安裝專案 CLI
pip install -e .
```

安裝完成後，即可直接在終端機使用 `pocket-agent` 命令。

---

### 首次設定精靈 (First-Time Setup)

無論採用哪種安裝方式，**初次使用建議先執行設定精靈**以配置模型供應商與認證（設定將儲存於 `~/.pocket-agent/config.json`）：

```bash
# 若使用手動安裝 (途徑 C)
pocket-agent setup

# 若使用一鍵腳本 (途徑 B)
run.bat setup       # Windows
./run.sh setup      # Linux
./run.command setup # macOS
```

設定精靈將以互動式選單引導您完成四個步驟：

1. **選擇服務供應商 (Provider)**：
   - **ChatGPT Plus/Pro (OpenAI OAuth PKCE)**：【免 API Key】系統將引導開啟瀏覽器，於官方登入頁授權後自動將 Token 回傳至本機 `http://localhost:1455`，直接調用進階模型。
   - **Gemini (Google AI Studio)**：支援 Google OAuth 瀏覽器登入或直接輸入 `GEMINI_API_KEY`。
   - **雲端推理平台 (OpenRouter / Groq / SambaNova / Mistral / Ollama Cloud)**：直接輸入對應的 API Key（敏感資訊將自動透過作業系統 Keyring 加密保護）。
   - **本機端開源模型 (Ollama)**：預設連線 `http://localhost:11434`，離線亦可運行。
2. **設定預設模型 (Model)**：
   - 精靈會嘗試動態向伺服器查詢可用模型清單供您挑選，亦支援自訂模型名稱。
3. **選配外部 MCP 工具**：
   - 詢問是否掛載額外的 Stdio / SSE MCP 伺服器（可直接按 Enter 略過，日後透過設定檔擴充）。
4. **啟動代理**：
   - 完成設定後，直接執行 `pocket-agent`（或執行 `run.bat` / `./run.sh`）即可開始對話！

---

## 💻 快速上手與日常操作 (Quick Start & Usage)

啟動後，終端機將呈現互動式對話面板。底端狀態列會即時顯示當前模型、推理強度、待送附件數與累計 Token 用量。

### 終端互動快捷鍵

| 按鍵組合                      | 功能說明                                                                                               |
| :---------------------------- | :----------------------------------------------------------------------------------------------------- |
| **Enter**                     | 送出當前輸入文字。                                                                                     |
| **Esc 後 Enter** (Meta+Enter) | 插入換行符號（多行貼上時亦會保留於編輯緩衝區）。                                                       |
| **Alt+V**                     | 從剪貼簿智慧貼上：支援檔案總管選取的實體檔案、文字路徑，以及**記憶體截圖（免存檔自動轉入待送附件）**。 |
| **Tab / 方向鍵**              | 候選詞自動補齊、在互動選單中移動光棒。                                                                 |
| **Ctrl+L**                    | 清除終端機畫面顯示，保留歷史對話脈絡。                                                                 |
| **Ctrl+C**                    | 輸入文字時：清空當前輸入框（不清除待送附件）；長任務執行時：中斷執行中的子代理或主代理。               |
| **Ctrl+D** 或 `/exit`         | 關閉並安全退出應用程式。                                                                               |

---

### 核心斜線指令 (Slash Commands)

您可以在輸入框隨時鍵入下列以 `/` 開頭的指令：

#### 🤖 模型與工作階段管理

- `/model`：啟動互動式選單，即時切換服務供應商與模型（支援 `openai`, `gemini`, `openrouter`, `groq`, `sambanova`, `mistral`, `ollama`, `ollamacloud`）。
- `/reasoning [auto|high|medium|low|none]` (或 `/effort`)：即時調整 LLM 推理強度（預設 `auto` 交由模型／供應商決定；不帶參數時開啟互動選單，目前狀態同步顯示於底部狀態列）。
- `/session`：啟動歷史對話光棒管理選單，支援分頁檢視、載入與刪除。
- `/new`：結束當前會話並開啟全新乾淨對話（自動原子性保存舊紀錄，完整重設上下文記憶、待送附件與用量）。
- `/rename <新標題>`：重新命名當前對話主題，便於日後在 `/session` 中識別。
- `/resume`（啟動參數）：若想在啟動時直接接續上次未完成對話，可執行 `pocket-agent --resume`。
- `/compact`：呼叫 AI 執行歷史對話摘要與修剪，快速回收 Context 視窗容量。
- `/clear`：僅清空記憶體中的對話歷史，保留當前待送附件與 Session ID。
- `/usage` (或 `/quota`)：顯示當前對話累計 Token 消耗量與上下文視窗水位。

#### 📂 檔案、工作區與上下文操作

- `/file <路徑>`：將本機檔案（文字檔、程式碼、PDF 等）加入待送附件佇列。對話中亦支援以 `@檔名` 進行行內引用。
- `/image <提示詞>`：呼叫圖像生成模型進行繪圖（若待送佇列有圖片附件，自動啟用圖生圖 / 影像編輯模式）。
- `/codesearch <關鍵字>` (或 `/cs`)：利用本機 SQLite FTS5 極速全文檢索工作區程式碼，並將關鍵片段隱式加入 Context。
- `/add-dir <路徑>`：授權並新增額外的工作目錄。
- `/rm-dir [路徑]`：移除已授權的工作目錄（不帶參數顯示互動選單）。
- `/attachments`：列出當前待送佇列的所有附件。
- `/rm-file [編號或路徑]`：移除單一待送附件（不刪除實體檔案）。
- `/undo`：檢視 AI 修改檔案之備份歷史差異，並支援互動式還原。

#### 👥 子代理與客製技能 (Subagents & Skills)

- `/subagent <name> <任務>` 或 `/<name> <任務>`：**Zero-Token 路由**——系統於底層直接攔截並啟動專屬子代理處理任務，不經主代理推論，零 Token 浪費（例如 `/researcher 搜集競品資料`）。
- `/parallel <name:task> | <name:task>`：平行啟動多個子代理進行分工（Map 階段），並由背景自動總結彙整成單一最終報告（Reduce 階段）。_注意：代理間請使用直線符號 `|` 分隔_。
- `/skills [name]`：列出當前所有可用的全域與專案專屬技能 (Skills)，或檢視特定技能 SOP。
- `/learn`：由 AI 分析當前對話脈絡，互動式萃取並存為可重複呼叫的新技能。

#### 🔧 系統設定與維護

- `/config`：啟動全域設定檔互動式編輯器。
- `/clean`：手動觸發自動管家 (Housekeeping)，批次清理過期日誌、臨時截圖與溢出暫存。
- `/update`：檢查並立即套用 GitHub 最新 Commit 更新，自動儲存並重啟（僅限 Modern Portable 版）。
- `/logout`：清除本機儲存的 OAuth 憑證。

> 💡 更多關於路徑解析、圖生圖細節及系統邊界行為，請詳閱：[CLI 操作與限制說明](docs/cli-usage-limitations.md)。

---

## ✨ 核心架構與特色 (Architecture & Features)

### 1. 雙軌大模型轉接架構 (LLM Engine Adapter Architecture)

- **OpenAI 核心 / OpenRouter 相容**：原生支援 ChatGPT Plus/Pro 最新協定，並相容於 OpenRouter，完美整合 Function Calling 與深度反思推理 (Reasoning)。
- **Ollama Chat Completions**：採用 Adapter Pattern 相容標準 API，內建 `<think>` 標籤狀態機剖析器。即使切換至本地開源推理模型 (如 DeepSeek-R1)，思考過程亦能無縫串流，並精準統計 Token 用量。
- **動態切換**：執行期間隨時透過 `/model` 指令秒級切換服務供應商與模型。

### 2. 本地文件產生與匯出 (Native Document Generation)

- **自動化文件排版**：無需依賴外網服務，內建 `create_docx`、`create_pdf` 與 `create_pptx` 原生工具。
- **無縫格式轉換**：透過自然語言指令，AI 即可自動整理專案脈絡，產出格式齊整的 Word 企劃書、PDF 說明文件或 PowerPoint 簡報。

### 3. 多重代理與擴充性 (Extensibility & Multi-Agent)

- **子代理 (Subagents)**：支援在設定中定義多元人設（如架構師、稽核員），複雜任務由主代理自動或透過 `/parallel` 委派處理。
- **事件鉤子 (Hooks)**：支援在 `before_send`, `after_response`, `before_tool`, `after_tool` 綁定本機 Python 腳本，實現自訂攔截邏輯。
- **MCP (Model Context Protocol)**：完整支援 Stdio 與 SSE，輕鬆掛載本機檔案系統、資料庫或遠端工具。隨附提供：
  - `browser_mcp.py`：純 Python 網頁自動化操作，直接沿用系統 Edge/Chrome，支援 CDP 連接與 Cookie 狀態繼承。
  - `github_mcp.py`：GitHub Issue / PR 檢索與留言互動。
  - `sqlite_mcp.py` / `mysql_mcp.py` / `mssql_mcp.py`：本機與遠端資料庫結構探查與唯讀查詢。
  - `memory_mcp.py`：基於本機 JSON 的長期跨對話記憶檢索。
  - `rhel_mcp.py`：遠端 Linux (RHEL) 主機 SSH 結構化唯讀巡檢與系統日誌診斷。
  - `win_rdp_mcp.py`：遠端 Windows 主機 RDP (Port 3389) 視覺控制與唯讀巡檢。
- **CLI 動態捷徑 (Zero-Token Routing)**：輸入 `/<name>` 直接啟動子代理，跳過主代理的 LLM 推理，達成零 Token 消耗的高速派發。

### 4. 企業級安全治理與工作區零污染 (Enterprise Governance)

- **CLI 指令執行確認**：當 AI 嘗試呼叫 Shell 執行指令時，強制彈出確認視窗 `[y: 單次 | a: 本次對話 | A: 加入白名單 | c: 自訂指示 | N: 拒絕]`，嚴防非預期動作。
- **MCP Default-Deny 授權**：外部 MCP 工具預設強制要求使用者確認，僅有列於 `safe_read_tools` 清單的唯讀操作方可免確認放行。
- **敏感資訊注入 (Inject) 與動態詢問**：連線密碼或 API Key 支援於執行期由終端機隱藏輸入（僅存於記憶體，不寫入 Context 與日誌），或透過 `inject` 自動抹除 LLM Schema 中的機密欄位。
- **指令沙盒 (Shell Sandbox)**：支援透過設定 `USE_SANDBOX=docker`，將終端機指令隔離於容器內執行。
- **Context Budget 上下文預算管線**：基於動態預算公式控管視窗水位，提供「工具巨量輸出落盤虛擬化 (Spillover)」、「歷史對話壓縮 (History Compactor)」與「工具輸出置換」多層防護，根除 Context Overflow。
- **工作區零污染 (Zero Project Pollution)**：所有執行期產生的工具溢出檔案、截圖暫存與日誌，一律自動隔離於全域 `~/.pocket-agent/workspaces/<workspace-slug>/` 沙盒中，專案目錄絕不產生未受追蹤的暫存檔案，確保 Git 狀態絕對乾淨。

### 5. 深入架構與實作文件 (Deep Dive Documentation)

如需深入探討 Pocket Agent 的架構設計，請參考隨附的技術規格文件：

- [OAuth 2.0 PKCE 認證機制](docs/oauth-mechanism.md)
- [雙軌大模型轉接架構 (LLM Engine Adapter)](docs/llm-adapter-architecture.md)
- [上下文預算管理架構 (Context Budget Architecture)](docs/context-management-architecture.md)
- [設定檔規格說明書 (Configuration Specification)](docs/configuration-specification.md)
- [分層配置與上下文探索機制 (Agent Configuration Standards)](docs/agent-configuration-standards.md)
- [MCP 伺服器掛載與安全注入機制](docs/mcp-architecture.md)
- [工具呼叫與 Subagent 委派機制](docs/tool-subagent-mechanism.md)
- [Hooks 生命週期機制](docs/hooks-lifecycle.md)
- [BM25 向量檢索與記憶機制](docs/bm25-visual-explanation.md)
- [敏感設定加密與 OS Keyring 整合](docs/sensitive-data-protection.md)
- [三層式路由架構 (Three-Tiered Routing)](docs/three-tier-routing-architecture.md)
- [子代理權限與工作區隔離報告](docs/agent-workspace-architecture-report.md)
- [CLI 操作與限制說明 (CLI Usage & Limitations)](docs/cli-usage-limitations.md)

---

## ⚙️ 進階設定與客製化 (Configuration & Customization)

### 全域系統設定 (`config.json`)

PocketAgent 的全域設定檔位於 `~/.pocket-agent/config.json`，採巢狀 JSON 格式。您可直接手動修改，或透過 CLI 的 `/config` 指令互動調整。常見設定項目：

```json
{
  "active_provider": "openai",
  "active_model": "gpt-6-astra",
  "system": {
    "max_tool_rounds": 20,
    "log_level": "WARNING",
    "use_sandbox": "none",
    "context_window": null,
    "context_reserve_output": 4096,
    "auto_update": true
  }
}
```

> 完整設定規格說明請參考：[設定檔規格說明書](docs/configuration-specification.md)。

---

### 分層配置與工作區規範 (`AGENTS.md`)

PocketAgent 支援階層式指令疊加，讓您無需修改全域設定，即可為個別專案定義專屬規範與技能：

```text
載入順序：全域常識 (~/.pocket-agent/instructions.md)
          ⬇ 疊加合併
          專案規範 (目標目錄/AGENTS.md 或 .agents/AGENTS.md)
          ⬇ 技能探索 (專案同名技能優先遮蔽全域技能)
          專案技能 (.agents/skills/*/SKILL.md)
```

1. **全域規範**：若有跨專案通用的開發原則（例如：一律使用繁體中文、撰寫型別註釋），請建立 `~/.pocket-agent/instructions.md`。每次啟動皆會自動作為基礎 System Prompt。
2. **專案入口規範 (`AGENTS.md`)**：在目標專案根目錄建立 `AGENTS.md`（或 `.agents/AGENTS.md`）。啟動時將自動附加於全域規範後方，並自動遞迴展開內容中所連結的專案 Markdown 檔案（累計上限 `24,000` 字元，超出部分改由 `read_file` 工具按需讀取）。
3. **專案自訂技能 (`.agents/skills/`)**：在專案中建立 `.agents/skills/<skill-name>/SKILL.md`，代理將優先感知並動態載入該技能，自動覆寫全域同名技能。

---

### 外部 MCP 伺服器掛載與注入

如需擴充工具，請複製專案提供的 [`mcp_config.example.yaml`](mcp_config.example.yaml) 為 `mcp_config.yaml`：

- **原始碼開發環境**：直接放置於專案根目錄 `mcp_config.yaml`（已受 `.gitignore` 保護）。
- **打包發布版 (Portable)**：放置於使用者全域目錄 `~/.pocket-agent/mcp_config.yaml`。

```yaml
servers:
  browser:
    transport: stdio
    command: python
    args: ['mcp_servers/browser_mcp.py']

  github:
    transport: stdio
    command: python
    args: ['mcp_servers/github_mcp.py']
    safe_read_tools:
      - github_list_prs
      - github_read_issue
    env:
      GITHUB_TOKEN: '' # 建議留空，首次呼叫時於終端機安全隱藏輸入
```

- **`safe_read_tools`（唯讀白名單）**：列入清單的工具可免除每次的 `[y/N]` 互動確認。
- **連線參數動態詢問**：`env` 內的帳號、密碼、Token 欄位若留空或省略，CLI 會在首次使用時於終端機提示輸入，僅暫存於記憶體，保護機密不外洩。

---

## 🛠️ 開發與貢獻 (Development & Contributing)

我們非常歡迎社群參與開發！在提交 Pull Request 前，請遵循以下開發規範：

### 1. 安裝開發依賴

本專案將「正式環境」與「開發測試環境」相依套件明確分離：

```bash
# 安裝開發與測試套件 (pytest, ruff 等)
pip install -r requirements-dev.txt
```

### 2. 依賴套件維護規範

- **直接相依來源**為 `requirements.in` 與 `requirements-dev.in`。
- **嚴禁手動修改** `requirements.txt` 與 `requirements-dev.txt`（這些檔案是由 `pip-compile` 自動產生的版本鎖定檔）。
- 若需新增或調整套件，請修改 `.in` 檔案後重新編譯：
  ```bash
  pip-compile requirements.in
  pip-compile requirements-dev.in
  ```

### 3. 靜態分析與程式碼風格檢查

本專案採用 **Ruff** 維護程式碼品質，行長上限嚴格為 **120 字元**，並要求 UTF-8 編碼與 LF 換行：

```bash
# 格式化檢查
python -m ruff format --check .

# Lint 規則檢查
python -m ruff check .

# 依賴完整性檢查 (掃描是否有未登記於 requirements.in 的 import)
python scripts/check_imports.py src scripts
python scripts/check_imports.py tests --req requirements-dev.in
```

### 4. 執行自動化測試

```bash
# 執行完整測試套件
python -m pytest tests/

# 執行特定測試
python -m pytest tests/test_cli_flow.py
```

---

## 📦 發布與建置 (Build & Deployment)

專案提供了統一的發布打包工具：

```cmd
build.bat
```

執行後將顯示模式選擇：

1. **新版 Portable (Modern) [推薦]**：免安裝的可攜式發佈包。將官方 Python 執行環境與相依套件完整打包至 `dist/PocketAgent-portable.zip`，避開防毒軟體誤判。使用者解壓後執行 `pocket.bat` 即可使用，且支援線上無縫自動更新。
2. **舊版 PyInstaller (Legacy)**：將專案編譯為單一二進位執行檔（可能觸發防毒軟體信譽隔離警告）。

---

## ⚠️ 免責聲明 (Disclaimer)

### 繁體中文

1. **按「現狀」提供且無任何擔保**：本專案及其隨附之所有元件（包含但不限於核心代理引擎、子代理、自動化腳本，以及所有內建與外掛之 MCP 伺服器，例如網頁自動化 `browser_mcp`、遠端 Linux 巡檢 `rhel_mcp`、遠端 Windows 桌面控制 `win_rdp_mcp`、資料庫工具與本機終端機執行工具）均依「現狀（AS IS）」提供，不附帶任何明示或默示之保證（包含但不限於適售性、特定用途適用性、正確性或無侵權保證）。
2. **AI 非確定性與操作風險自負**：大型語言模型（LLM）本質上具有非確定性，可能產生幻覺（Hallucination）、誤判上下文或執行非預期之操作（包含但不限於修改或刪除本機／遠端檔案、變更系統組態、執行系統命令、操控遠端桌面或瀏覽器工作階段、讀寫資料庫，以及產生第三方 API 額度或費用消耗）。使用者應於受控環境中審慎使用、親自核對每一次權限確認提示，並完全自行承擔所有操作風險與資料備份責任。
3. **第三方服務條款與合規責任**：使用者透過本工具串接任何第三方服務或驗證機制（如 OpenAI、Google、OpenRouter 或其他模型供應商）時，應自行確認並遵守各該平台之服務條款（Terms of Service）、使用政策、企業資安規範及所在地法令。若因使用本工具導致第三方帳號受限、停權或違反相關規定，概由使用者自行負責。
4. **責任限制**：在適用法律允許之最大範圍內，無論基於契約、侵權行為或其他法律理論，原作者與貢獻者對於使用者或任何第三方因使用、誤用、依賴或無法使用本軟體及其內建／外掛 MCP 工具所生之任何直接、間接、附帶、特別、衍生性或懲罰性損害（包含但不限於資料遺失或毀損、系統故障、業務中斷、資安事件、帳號處分、金錢損失或第三方索賠），縱使事前已被告知該等損害發生之可能性，**均不負任何法律責任或賠償義務，一切後果皆與原作者無關**。一旦下載、安裝、執行或以任何方式使用本專案，即表示您已完整閱讀、理解並同意上述所有條款。

### English

1. **Provided "AS IS" Without Warranty**: This software and all bundled or associated components (including, without limitation, the core agent engine, subagents, automation scripts, and all built-in or external MCP servers such as `browser_mcp`, `rhel_mcp`, `win_rdp_mcp`, database connectors, and local shell execution tools) are provided **"AS IS"** and **"AS AVAILABLE"**, without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, reliability, accuracy, or non-infringement.
2. **AI Non-Determinism & Assumption of Risk**: Large Language Models (LLMs) are inherently non-deterministic and may hallucinate, misinterpret instructions, or trigger unintended actions (including, without limitation, modifying or deleting local or remote files, altering system configurations, executing shell commands, interacting with browser sessions or remote desktops, mutating databases, or incurring third-party API usage charges). Users assume full responsibility for reviewing every permission prompt, maintaining proper backups, and running this tool solely within authorized and controlled environments.
3. **Third-Party Terms & Legal Compliance**: Users are solely responsible for ensuring that their use of this tool—including any OAuth flows, API integrations, remote host connections, or automated interactions—complies with all applicable laws, organizational security policies, and third-party Terms of Service (such as OpenAI, Google, OpenRouter, or other providers). Any account restrictions, suspensions, or policy violations resulting from the use of this software are strictly the user's own responsibility.
4. **Limitation of Liability**: To the maximum extent permitted by applicable law, in no event shall the original author(s) or contributors be liable for any direct, indirect, incidental, special, exemplary, or consequential damages (including, without limitation, data loss or corruption, security incidents, unauthorized operations, financial loss, or third-party claims) arising out of or in connection with the use, misuse, or inability to use this software or any of its built-in/external MCP tools, even if advised of the possibility of such damages. **All unintended behaviors and consequences rest solely with the user and have no relation to the original author(s).** By downloading, installing, running, or otherwise using this software, you acknowledge that you have read, understood, and agreed to this Disclaimer.

---

## 📜 授權條款 (License)

本專案採用 [MIT License](LICENSE) 授權條款釋出。詳細內容請參閱本儲存庫根目錄的 `LICENSE` 檔案。
