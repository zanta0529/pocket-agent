# PocketAgent 🤖

[![Python Version](https://img.shields.io/badge/python-3.14%2B-blue.svg)](https://www.python.org/)
[![Code style: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](#)

## 📖 專案簡介 (Introduction)

**PocketAgent** 是一款具備企業級擴充性與安全性的本機端 Python AI 代理工具，提供極簡、高效且專注於開發者體驗的命令列 (CLI) 介面。

本專案的核心優勢在於採用了 **OAuth PKCE 認證直接串接 ChatGPT 後端 (Codex API)**，這意味著只要您擁有 ChatGPT Plus/Pro 帳號，即可免費運用進階模型與深度的邏輯推理 (Reasoning) 能力，完全不需要申請或消耗 OpenAI API Key 的額外計費餘額。同時，專案也完美相容於 **OpenRouter API**，並內建強大的本地端大語言模型 (Ollama) 支援，滿足對資料隱私具高度要求的離線開發場景。

---

## ✨ 核心架構與特色 (Architecture & Features)

### 1. 雙軌大模型轉接架構 (LLM Engine Adapter Architecture)

- **OpenAI 核心 / OpenRouter 相容**：原生支援 ChatGPT Plus/Pro 最新協定，並完全相容於 OpenRouter，完美整合工具呼叫 (Function Calling) 與反思推理 (Reasoning) 能力。
- **Ollama Chat Completions**：底層採用 Adapter Pattern (轉接器模式) 平滑相容標準 API，具備內建的 `<think>` 標籤狀態機剖析器。即使切換至本地端開源推理模型 (如 DeepSeek-R1)，亦能將思考過程無縫對接，並精準擷取 Token 用量。
- **動態切換**：可直接透過 CLI 的 `/model` 指令，於運行中即時互動切換服務提供者與大模型。

### 2. 本地文件產生與匯出 (Native Document Generation)

- **自動化文件排版**：無需依賴外部服務，內建 `create_docx`、`create_pdf` 與 `create_pptx` 原生工具。
- **無縫格式轉換**：只需透過自然語言下達指令，AI 即可自動整理對話脈絡、分析專案內容，並輸出排版精美的 Word 報告、PDF 說明書或 PowerPoint 簡報，大幅提升生產力。

### 3. 多重代理與擴充性 (Extensibility & Multi-Agent)

- **子代理 (Subagents)**：允許在設定檔定義多種人設 (例如：架構師、研究員)，主 Agent 在面對複雜情境時會自動委派任務，實現多代理協同作業。
- **事件攔截與鉤子 (Hooks)**：支援在 `before_send`, `after_response`, `before_tool`, `after_tool` 綁定並執行本機腳本，達成高度客製化。
- **MCP (Model Context Protocol)**：完整支援 Stdio 與 SSE，輕鬆掛載本地檔案系統、資料庫或遠端工具。自帶 `browser_mcp.py` 支援網頁自動化瀏覽與 Session 狀態繼承。
- **CLI 動態捷徑 (Zero-Token Routing)**：除了主 Agent 自行決定外，使用者可直接輸入 `/subagent <name>` 或動態捷徑 `/<name>`。系統會**攔截指令並直接在底層啟動子代理**，完美跳過主代理的 LLM 推理，達成零 Token 消耗的高速派發。

### 4. 企業級安全治理 (Enterprise Governance)

- **CLI 指令執行確認**：在終端機模式下，當 AI 嘗試呼叫 Shell 工具執行系統指令時，會強制暫停並提示使用者輸入 `[y/N]` 進行確認，防止不可逆的危險操作。
- **指令沙盒 (Shell Sandbox)**：透過啟用 `.env` 中的 `USE_SANDBOX=wsl` 或 `docker`，徹底隔離 AI 的終端機執行權限。
- **Artifacts 全域隔離**：強制將 AI 自動生成的專案計畫與文檔寫入 `~/.pocket-agent/artifacts/`，避免污染既有專案程式碼。
- **Context Overflow 滑動視窗與防護網**：當上下文超過 12 萬字元時，自動觸發首尾保留截斷機制。若模型 API 仍拋出超載錯誤，系統會自動攔截崩潰、退回該次輸入，並注入 `[System]` 提示指導 AI 改用平行子代理或精準搜尋，確保對話永不中斷。
- **程式碼檢索增強 (RAG Codebase Search)**：內建 `indexed_search` 工具，利用本地 SQLite FTS5 建立輕量級程式碼庫索引，取代低效的 `grep_search`，實現極速且精準的片段擷取。

### 5. 工作區環境規範 (Workspace Context)

支援在專案目錄下放置 `AGENTS.md` 或 `.subagents/AGENTS.md` 檔案。CLI 啟動時會自動偵測並尋找第一順位的檔案，將其內容注入為 System Prompt 的一部分。此設計讓開發者可針對不同專案設定專屬的程式碼風格、架構限制與 AI 規範（或建立 Thin Router 引導 AI 載入外部指令），而無需修改全域設定。

### 6. 深入架構與實作文件 (Deep Dive Documentation)

為了幫助開發者深入了解 Pocket Agent 底層的實作細節與設計模式，我們準備了以下視覺化說明文件：

- [OAuth 2.0 PKCE 認證機制](docs/oauth-mechanism.md)
- [雙軌大模型轉接架構 (LLM Engine Adapter)](docs/llm-adapter-architecture.md)
- [Hooks 生命週期機制](docs/hooks-lifecycle.md)
- [MCP 伺服器掛載與安全注入機制](docs/mcp-architecture.md)
- [工具呼叫與 Subagent 委派機制](docs/tool-subagent-mechanism.md)
- [Subagent 與 Skill 差異比較](docs/subagents-skills-comparison.md)
- [分層配置與上下文探索機制 (Agent Configuration Standards)](docs/agent-configuration-standards.md)
- [BM25 向量檢索與記憶機制](docs/bm25-visual-explanation.md)
- [CLI 操作與限制說明 (CLI Usage & Limitations)](docs/cli-usage-limitations.md)

### 7. 打包版本的專案客製化 (Customizing Packaged Versions)

當您使用經過打包的發布版本（例如 `PocketAgent-portable.zip` 內的執行檔）時，您可以透過**「分層配置 (Layered Configuration)」**來完美隔離全域與專案的 AI 規範：

#### A. 全域規範 (Global Instructions)

若您有跨專案通用的開發習慣（例如：一律使用繁體中文、要求撰寫 Docstring），請建立：
📄 `~/.pocket-agent/instructions.md` (Windows 通常位於 `C:\Users\使用者名稱\.pocket-agent\instructions.md`)
PocketAgent 會在每次啟動時自動載入此檔案，作為底層的通用規則。

#### B. 專案特有規範 (Project-Local Configurations)

請在**您當下欲開發的目標專案工作目錄** 中，依循以下結構配置專案專屬的限制與技能：

1. **專案入口規範 (`AGENTS.md`)**
   在您的專案根目錄建立 `AGENTS.md`（或 `.subagents/AGENTS.md`）。啟動時，它會被附加在全域規範之後，從而精準覆寫全域規則（例如：特定專案強制使用 TypeScript）。
2. **自訂技能清單 (`.subagents/skills/`)**
   建立 `.subagents/skills/` 資料夾，並在其中以子目錄形式建立技能（例如 `.subagents/skills/my-workflow/SKILL.md`）。AI 代理將會自動感知並優先載入此處的技能，覆寫全域同名的預設技能。

💡 **運作原理範例**：
假設您把 `PocketAgent.exe` 安裝在全域環境變數中。當您透過終端機 `cd D:\Projects\MyAwesomeApp` 並執行 PocketAgent 時，它會自動將 `~/.pocket-agent/instructions.md` 與 `D:\Projects\MyAwesomeApp\AGENTS.md` 堆疊合併，同時優先讀取該目錄下的專案專屬技能，從而完美實現**不改動執行檔即可跨專案無縫切換上下文**的隔離體驗。

---

## 🛠️ 環境需求與安裝 (Prerequisites & Installation)

### 系統需求

- **Python 3.14 或以上版本**
- 若於 Windows 環境執行，請確保安裝 Python 時勾選了 `tcl/tk` 以支援 Tkinter GUI 元件。

### 快速啟動腳本 (一鍵自動化)

專案內建各平台的啟動腳本，將自動偵測環境、建立虛擬環境 (venv)、安裝依賴並啟動應用程式。初次啟動將引導開啟瀏覽器進行 OAuth 授權。

**🍎 macOS**

```bash
chmod +x agent.command
./agent.command
```

**🐧 Linux**

```bash
chmod +x agent.sh
./agent.sh
```

**🪟 Windows**

```cmd
agent.bat
```

### 開發者手動啟動

```bash
python -m venv venv
source venv/bin/activate  # Windows 環境請執行: venv\Scripts\activate
pip install -r requirements.txt
```

若您首次使用，建議先執行設定精靈完成 API 參數與登入配置（將儲存於 `~/.pocket-agent/config.json`）：

```bash
python -m pocket_agent setup
```

完成設定後，即可啟動代理：

```bash
python -m pocket_agent
```

---

## 💻 快速上手與操作 (Quick Start & Usage)

啟動後，您可以在命令列輸入框直接使用下列核心指令 (Slash Commands)：

- `/file <路徑>`：附加本機檔案（支援程式碼、PDF 等）作為上下文。
- `/image <描述>`：呼叫模型進行圖像生成 (可附加參考圖作為 Context 進行圖生圖)。
- `/model <provider> <model_name>`：即時切換後端 API (支援 `openai`、`openrouter` 或 `ollama`)。
- `/subagent <name>`：強制委派任務給指定的子代理（例如 `/subagent researcher 查資料`）。另外也支援直接使用 `/<name>` 作為動態捷徑（例如 `/researcher 查資料`）。
- `/parallel <name:task> | <name:task>`：手動平行啟動多個子代理 (Map-Reduce 模式)。背景會自動彙整成單一最終報告。請留意**必須使用管線符號 `|` 作為分隔**。例如：`/parallel researcher:查庫存 | coder:寫測試`。
- `/session [list | load <id> | save | delete <id>]`：管理歷史對話 (Session)。無參數時啟動互動式光棒選單，支援儲存與讀取持久化紀錄。
- `/skills [name]`：列出目前可用的所有全域與專案技能，或檢視特定技能內容。
- `/learn`：自動萃取當前對話中的知識或流程，互動式引導將其儲存為可重複使用的 Skill。
- `/config`：啟動互動式設定選單，用以編輯全域設定與 API Key。
- `/undo`：啟動互動式還原選單，可檢視檔案備份差異並執行還原。
- `/add-dir <路徑>` / `/workspace`：授權並新增額外的工作目錄。
- `/compact`：讓 AI 自動摘要並壓縮中間的對話歷史，快速回收 Context 上限空間。
- `/clear`：清除當前記憶體的對話歷史。
- `/logout`：清除本機端的 OAuth Token 憑證。
- `/quit`：關閉應用程式。

若想在啟動時自動載入前一次未完成的對話，可附加 `--resume` 參數：

```bash
python -m pocket_agent --resume
```

命令列介面採可捲動對話版面；首屏只列目前專案、模型與 MCP 工具數，詳細清單按需查看。底端狀態列（Toolbar）會即時顯示當次對話累計的 Token 用量。

| 操作                       | 說明                                           |
| -------------------------- | ---------------------------------------------- |
| Enter                      | 送出整段輸入                                   |
| Esc 後 Enter（Meta+Enter） | 插入換行；多行貼上先保留於編輯器               |
| Tab／方向鍵                | 補全與選擇光棒選單；輸入歷史保留               |
| Ctrl+L                     | 清除終端機畫面顯示，但不清除對話歷史           |
| Ctrl+C（輸入期間）         | 有文字時清空輸入，空白時提示退出方式；不清附件 |
| Ctrl+D（空輸入）或 /quit   | 離開                                           |
| /help、/tools              | 查看指令、MCP 工具清單                         |
| /subagent、/<name>         | 委派任務給單一子代理（例如 /researcher）           |
| /parallel                  | 平行委派給多個子代理，使用 `\|` 分隔 (需注意分隔符號) |
| /model                     | 互動式光棒選單切換提供者與模型，Esc 返回       |
| /session                   | 互動式光棒選單分頁瀏覽並管理歷史對話           |
| /skills                    | 列出或檢視可用的技能 (Skills)                  |
| /learn                     | 將當前對話萃取為新技能                         |
| /config                    | 互動式編輯系統設定檔                           |
| /undo                      | 互動式檢視檔案備份並執行還原                   |
| /add-dir                   | 授權並新增工作目錄                             |
| /compact                   | 自動摘要並壓縮對話紀錄                         |
| /attachments               | 列出本次待送附件與編號                         |
| /attachments remove <編號> | 移除單一待送附件，不刪除檔案                   |
| /attachments clear         | 清空待送佇列，不刪除檔案                       |

> 💡 關於 CLI 介面的附件處理、路徑解析、影像生成細節，以及系統錯誤與中斷時的邊界行為，請詳閱：[CLI 操作與限制說明](docs/cli-usage-limitations.md)。

---

## ⚙️ 進階設定 (Advanced Configuration)

### 全域系統設定 (Global Configuration)

PocketAgent 的全域設定自動產生並存放在 `~/.pocket-agent/config.json` 中，採用結構化的巢狀 JSON 格式。您可以直接編輯該檔案，或是使用 CLI 的 `/config` 指令進行互動式修改（支援使用點表示法，如 `system.log_level`）。
預設包含以下核心參數：
- `system.max_tool_rounds`：預設為 `20`，控制模型單次對話可連續呼叫工具的最高次數，防止因幻覺導致無限迴圈。
- `system.log_level`：預設為 `"WARNING"`。若需深入追查 API 請求或除錯，可將其改為 `"DEBUG"` 或 `"INFO"`。
- `active_provider` / `active_model`：當前預設的服務提供者與模型名稱。

### 外部 MCP 伺服器掛載

如需橋接外部工具或資料庫，請複製根目錄的 `mcp_config.example.yaml` 並重新命名為 `mcp_config.yaml`（該檔已受 `.gitignore` 保護，避免機密外流）：

```yaml
servers:
  browser:
    transport: stdio
    command: python
    args: ['mcp_servers/browser_mcp.py']
```

_(💡 提示：若工具需使用敏感的 API Key，可利用 `inject` 參數配置，PocketAgent 會在底層自動注入，確保模型層不會接觸到金鑰明文。)_

---

## 🛠️ 開發與貢獻 (Development & Contributing)

本專案提供了一系列輔助工具來維持程式碼的品質與穩定性，包含針對依賴套件管理的專屬檢查腳本。

### 本機文件解析工具 (`convert_to_md.py`)

在將內部文件或外部參考資料放入 RAG (檢索增強生成) 資料庫，或是餵給 AI 作為上下文之前，將二進位文件無損轉換為純文字是非常重要的一環。`scripts/convert_to_md.py` 是一個零成本的命令列小工具，底層直接呼叫了專案內建的 `read_document` 引擎，可以在本地端瞬間將文件轉換為精準的 Markdown 格式（支援表格完美還原）。

**支援格式**：`.pdf`, `.docx`, `.pptx`

**使用方式**：
```bash
python scripts/convert_to_md.py <輸入檔案路徑> [-o <輸出檔案路徑>]

# 範例 (自動在同目錄產生 N3A_1_DM.md)：
python scripts/convert_to_md.py D:\_Personal\Downloads\insurance\N3A_1_DM.pdf
```

### 依賴套件檢查工具 (`check_imports.py`)

為了避免開發者在撰寫新功能時引用了未記錄的第三方套件，專案內建了靜態分析腳本 `scripts/check_imports.py`。該腳本會掃描目錄下的所有 Python 檔案，並與 `requirements.in` 進行比對，以確保沒有遺漏的依賴。本專案將「正式環境」與「開發測試環境」分離，因此在 CI 流程中會分別進行檢查。

**優點**：

- **預防套件遺漏 (Missing Dependencies)**：利用 AST 靜態分析所有的 `import`，精準攔截未註冊的套件。
- **自動化防呆**：內建 PyPI 名稱轉換對應（例如 `PIL` -> `pillow`），減少誤判。
- **CI/CD 無縫整合**：已經整合至專案的 GitHub Actions 與 Git Pre-commit hook 中。當發現遺漏時，會自動回傳 Exit Code `1` 阻擋 Commit 或是讓 CI 亮紅燈。

**使用方式**：

```bash
python scripts/check_imports.py [掃描目錄...] [參數]

# 範例 1：檢查正式環境 (Production) 依賴
python scripts/check_imports.py src scripts

# 範例 2：檢查測試環境 (Development) 依賴
python scripts/check_imports.py tests --req requirements-dev.in
```

**參數與引數**：

- `[掃描目錄...]`：(位置引數) 指定要掃描的原始碼目錄 (預設為 `src`)。支援傳入多個目錄。
- `--req <路徑>`：指定要比對的 requirements 檔案 (預設為 `requirements.in`)。
- `--ignore <套件名稱>`：忽略特定套件的檢查警告 (支援傳入多個名稱，以空白分隔)。

---

## 📦 發布與建置 (Build & Deployment)

專案提供免安裝 Python 依賴的可攜帶版本 (Portable) 打包方案，適合在無法連上外網的內網環境中快速部署：

```cmd
build.bat
```

執行後，腳本將自動將運行環境與所有相依套件打包至 `dist/PocketAgent-portable.zip`，使用者解壓縮後雙擊即可直接執行。

---

## 📜 授權條款 (License)

Copyright (c) 2026. All rights reserved.
