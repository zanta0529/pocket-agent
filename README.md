# PocketAgent 🤖

[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Code style: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)](#)

**PocketAgent** 是一款強大的本機端 Python AI 代理工具，具備圖形化介面 (GUI) 與命令列 (CLI) 雙模式。
最獨特的是，它採用 **OAuth PKCE 認證直接串接 ChatGPT 後端 (Codex API)**，這意味著**只要您擁有 ChatGPT Plus/Pro 帳號，即可免費使用進階模型與思考推理能力，完全不需要申請或消耗 OpenAI API Key 餘額**！

---

## ✨ 核心特色 (Key Features)

### 🔓 零 API Key 整合 (Zero-API-Key ChatGPT Plus/Pro)

- 直接呼叫 `chatgpt.com/backend-api/codex`，透過瀏覽器 OAuth 登入，完美利用您已訂閱的配額。
- 原生支援 **Reasoning Models (具備思考與反思能力的模型)**，並在 GUI 介面上即時串流顯示 `💭 思考過程`。

### 🧩 極致的擴充與自動化 (Automation & Extensibility)

- **子代理 (Subagents)**：在設定介面中定義特定人設的子代理（如：研究員、校稿員），主 Agent 會在複雜對話中**自動判斷並委派任務**給它們，實現多重代理協作。
- **事件攔截 (Hooks)**：支援在 `before_send`、`after_response`、`before_tool`、`after_tool` 綁定並自動執行 CLI 腳本，所有上下文將透過 `$POCKETAGENT_*` 環境變數傳遞。
- **快捷指令 (Slash Commands)**：自定義 `/指令`，快速展開預設的 Prompt 模板（支援 `$ARGUMENTS` 參數解析）。

### 🔌 MCP 與內建工具 (Tools & MCP Support)

- **完整的 MCP (Model Context Protocol)**：支援 Stdio 與 SSE，輕鬆掛載本地檔案系統、資料庫或遠端工具。
- **本機檔案與 Shell 控制**：賦予 AI 讀寫文件、執行本機終端機指令（跳出確認視窗防護）的能力。
- **內建瀏覽器控制**：自帶 `browser_mcp.py`，支援自動化網頁瀏覽，甚至能掛載您現有的 Chrome/Edge 狀態（保留登入 Cookie）。
- **進階文檔與生圖**：支援上傳 PDF 解析，並內建呼叫 `/image` 生成圖片並直接存檔。

---

## 🚀 快速上手 (Quick Start)

### 系統需求

- **Python 3.11+**
- (Windows 環境) 確保安裝 Python 時勾選 `tcl/tk` 以支援 GUI。

### 啟動專案 (一鍵自動設定)

我們提供了各平台專屬的一鍵啟動腳本，會自動幫您偵測 Python、建立虛擬環境 (venv)、安裝依賴並啟動應用程式。
*(初次啟動後將自動引導開啟瀏覽器進行 ChatGPT 帳號授權，成功後即可關閉瀏覽器回到 Agent)*

**🍎 macOS**
初次使用需賦予執行權限，之後即可在 Finder 中直接雙擊 `agent.command` 啟動：
```bash
chmod +x agent.command
./agent.command
```

**🐧 Linux (RHEL / Ubuntu 等)**
```bash
chmod +x agent.sh
./agent.sh
```

**🪟 Windows**
直接雙擊執行或在終端機輸入：
```cmd
agent.bat
```
*(💡 若希望啟動時隱藏後台的終端機黑視窗，可將 `agent.bat` 內的 `python.exe` 替換為 `pythonw.exe`)*

### 手動啟動 (開發者模式)

```bash
python -m venv venv
source venv/bin/activate  # Windows 請用 venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

---

## 💻 使用指南 (Usage)

啟動後，除了 GUI 面板上的按鈕外，您也可以直接在對話框輸入以下內建指令：

- `/file <路徑>`：附加本機檔案（包含 PDF 等）作為上下文。
- `/image <描述>`：呼叫 AI 進行圖像生成 (可搭配 `-i` 引入參考圖作圖生圖)。
- `/clear`：清除當前的對話歷史。
- `/logout`：清除本機登入 Token 憑證。
- `/quit`：關閉應用程式。

### 終端機模式 (CLI)

如果您偏好純文字終端機介面，請加上 `--cli` 參數：

```bash
python main.py --cli
```

---

## ⚙️ MCP 伺服器配置 (MCP Configuration)

若要掛載外部 MCP 伺服器，請將根目錄的 `mcp_config.example.yaml` 複製並重新命名為 `mcp_config.yaml`（已加入 `.gitignore` 保護）：

```yaml
servers:
  browser:
    transport: stdio
    command: python
    args: ['browser_mcp.py']
```

_進階技巧：如果 MCP 工具需要認證金鑰，可配置 `inject` 參數，PocketAgent 會自動注入給工具，而不會讓模型看到您的金鑰明文。_

---

## 📦 發佈與建置 (Build & Portable Deployment)

本專案提供免裝 Python 的可攜帶版本打包方案：

```cmd
build.bat
```

腳本將會自動把整個環境與相依套件打包至 `dist/PocketAgent-portable.zip`，解壓縮後雙擊即可立刻執行。

---

## 📜 授權條款 (License)

Copyright (c) 2026. All rights reserved.
