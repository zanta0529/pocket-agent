# PocketAgent 🤖

[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Code style: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](#)

## 📖 專案簡介 (Introduction)

**PocketAgent** 是一款具備企業級擴充性與安全性的本機端 Python AI 代理工具，提供圖形化介面 (GUI) 與命令列 (CLI) 雙模式。

本專案的核心優勢在於採用了 **OAuth PKCE 認證直接串接 ChatGPT 後端 (Codex API)**，這意味著只要您擁有 ChatGPT Plus/Pro 帳號，即可免費運用進階模型與深度的邏輯推理 (Reasoning) 能力，完全不需要申請或消耗 OpenAI API Key 的額外計費餘額。同時，專案內建強大的本地端大語言模型 (Ollama) 支援，滿足對資料隱私具高度要求的離線開發場景。

---

## ✨ 核心架構與特色 (Architecture & Features)

### 1. 雙軌大模型轉接架構 (LLM Engine Adapter Architecture)

- **OpenAI Responses API**：原生支援 ChatGPT Plus/Pro 最新協定，完美整合工具呼叫 (Function Calling) 與反思推理 (Reasoning) 能力。
- **Ollama Chat Completions**：底層採用 Adapter Pattern (轉接器模式) 平滑相容標準 API，具備內建的 `<think>` 標籤狀態機剖析器。即使切換至本地端開源推理模型 (如 DeepSeek-R1)，亦能將思考過程無縫對接至 GUI 的獨立摺疊面板中，並精準擷取 Token 用量。
- **動態切換**：可直接透過 GUI 的「帳號設定」介面或是 CLI 的 `/model` 指令，於運行中即時切換服務提供者與大模型。

### 2. 優化的使用者體驗 (Optimized UX)

- **獨立的思考過程顯示**：AI 的推理過程 (Thought) 獨立於對話內容顯示於 GUI 摺疊區塊，確保主對話窗乾淨易讀。
- **一鍵複製與匯出**：對話泡泡底部內建獨立的訊息與程式碼複製功能，一鍵帶走需要的產出。

### 3. 多重代理與擴充性 (Extensibility & Multi-Agent)

- **子代理 (Subagents)**：允許在設定檔定義多種人設 (例如：架構師、研究員)，主 Agent 在面對複雜情境時會自動委派任務，實現多代理協同作業。
- **事件攔截與鉤子 (Hooks)**：支援在 `before_send`, `after_response`, `before_tool`, `after_tool` 綁定並執行本機腳本，達成高度客製化。
- **MCP (Model Context Protocol)**：完整支援 Stdio 與 SSE，輕鬆掛載本地檔案系統、資料庫或遠端工具。自帶 `browser_mcp.py` 支援網頁自動化瀏覽與 Session 狀態繼承。

### 4. 企業級安全治理 (Enterprise Governance)

- **指令沙盒 (Shell Sandbox)**：透過啟用 `.env` 中的 `USE_SANDBOX=wsl` 或 `docker`，徹底隔離 AI 的終端機執行權限，規避不可逆的本機破壞。
- **Artifacts 全域隔離**：強制將 AI 自動生成的專案計畫與文檔寫入 `~/.pocket-agent/artifacts/`，避免污染既有專案程式碼。
- **Context Overflow 滑動視窗**：當上下文超過 12 萬字元時，自動觸發首尾保留截斷機制，防止 Token 上限導致的模型崩潰。

### 5. 深入架構與實作文件 (Deep Dive Documentation)

為了幫助開發者深入了解 Pocket Agent 底層的實作細節與設計模式，我們準備了以下視覺化說明文件：

- [OAuth 2.0 PKCE 認證機制](docs/oauth-mechanism.md)
- [雙軌大模型轉接架構 (LLM Engine Adapter)](docs/llm-adapter-architecture.md)
- [Hooks 生命週期機制](docs/hooks-lifecycle.md)
- [MCP 伺服器掛載與安全注入機制](docs/mcp-architecture.md)
- [工具呼叫與 Subagent 委派機制](docs/tool-subagent-mechanism.md)

---

## 🛠️ 環境需求與安裝 (Prerequisites & Installation)

### 系統需求

- **Python 3.11 或以上版本**
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

_(💡 提示：若希望隱藏後台的終端機視窗，可將 `agent.bat` 內的 `python.exe` 替換為 `pythonw.exe`)_

### 開發者手動啟動

```bash
python -m venv venv
source venv/bin/activate  # Windows 環境請執行: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

---

## 💻 快速上手與操作 (Quick Start & Usage)

啟動後除了操作 GUI 面板，您亦可在文字輸入框直接使用下列核心指令 (Slash Commands)：

- `/file <路徑>`：附加本機檔案（支援程式碼、PDF 等）作為上下文。
- `/image <描述>`：呼叫模型進行圖像生成 (可搭配 `-i` 參數引入參考圖，進行圖生圖)。
- `/model <provider> <model_name>`：即時切換後端 API (支援 `openai` 或 `ollama`)。
- `/session [list | load <id> | save | delete <id>]`：管理歷史對話 (Session)，支援儲存與讀取持久化紀錄。
- `/clear`：清除當前記憶體的對話歷史。
- `/logout`：清除本機端的 OAuth Token 憑證。
- `/quit`：關閉應用程式。

### 終端機模式 (CLI Mode)

若您習慣於無頭 (Headless) 或純文字環境中工作，請加上 `--cli` 參數啟動：

```bash
python main.py --cli
```

CLI 採可捲動對話版面；首屏只列目前專案、模型與 MCP 工具數，詳細清單按需查看。

| 操作                       | 說明                                                     |
| -------------------------- | -------------------------------------------------------- |
| Enter                      | 送出整段輸入                                             |
| Esc 後 Enter（Meta+Enter） | 插入換行；多行貼上先保留於編輯器                         |
| Tab／方向鍵                | 補全與選擇；輸入歷史保留                                 |
| Ctrl+C（輸入期間）         | 有文字時清空輸入，空白時提示退出方式；不清附件           |
| Ctrl+D（空輸入）或 /quit   | 離開                                                     |
| /help、/tools              | 查看指令、MCP 工具清單                                   |
| /model                     | 分步輸入／補全提供者與模型，Esc 返回；也保留完整參數語法 |
| /session                   | 管理歷史對話 (list / load / save / delete)               |
| /attachments               | 列出本次待送附件與編號                                   |
| /attachments remove <編號> | 移除單一待送附件，不刪除檔案                             |
| /attachments clear         | 清空待送佇列，不刪除檔案                                 |

使用 `/file <完整路徑>` 附加一般檔案，路徑可加雙引號。訊息內也可使用 `@README.md`；
含空白的 @ 路徑必須加雙引號，例如 `@"D:\my project\說明.txt" 請整理`。
檔案正規化去重，僅在送出時讀取內容；工具列的「待送」不代表模型已讀取。
`/clear` 只清對話並保留附件；`/add-dir` 尚未接入，不會讀取目錄或擴大工作權限。
CLI `/image <描述>` 使用待送附件作參考圖，不使用上方 GUI 語法的 `-i` 選項。
模型切換只對當次會話有效，不寫設定；僅 openai 顯示生圖指令。

**失敗與取消限制：** 聊天／生圖拋出例外時保留附件，原輸入預填供修改，不自動重送。
本輪可能已寫入歷史或執行部分工具，重送前請檢查結果。核心若將錯誤轉為正常回覆，
CLI 仍會消耗本次附件；不依回覆文字猜測成功。不提供核心交易回滾或 exactly-once 保證。
本批未接入回答串流或執行中安全取消，Ctrl+C 的清空行為僅適用於輸入期間。
無色彩輸出仍有文字狀態；Windows Terminal 的深／淺底、多行貼上及窄視窗需人工驗收。

---

## ⚙️ 進階設定 (Advanced Configuration)

### 外部 MCP 伺服器掛載

如需橋接外部工具或資料庫，請複製根目錄的 `mcp_config.example.yaml` 並重新命名為 `mcp_config.yaml`（該檔已受 `.gitignore` 保護，避免機密外流）：

```yaml
servers:
  browser:
    transport: stdio
    command: python
    args: ['browser_mcp.py']
```

_(💡 提示：若工具需使用敏感的 API Key，可利用 `inject` 參數配置，PocketAgent 會在底層自動注入，確保模型層不會接觸到金鑰明文。)_

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
