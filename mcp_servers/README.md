# Pocket Agent MCP 工具擴充指南

本目錄 (`mcp_servers/`) 包含了專為 Pocket Agent 設計與擴充的 Model Context Protocol (MCP) 伺服器腳本。
透過這些擴充，AI 代理可以安全地與您的本機環境、外部服務或瀏覽器進行互動。

## ⚙️ 如何啟用這些 MCP？

Pocket Agent 是透過讀取根目錄下的 `mcp_config.yaml` 來決定掛載哪些 MCP 伺服器。
請參考 `mcp_config.example.yaml`，將您需要的服務設定複製過去並解除註解，接著啟動 `agent` 即可。

---

## 🛠️ 目前提供的 MCP 伺服器清單

### 1. 🌐 瀏覽器自動化 (Browser MCP)

**檔案：** `browser_mcp.py`
**用途：** 讓 AI 具備開啟網頁、點擊按鈕、輸入文字等操作真實瀏覽器的能力。
**特色：**

- 純 Python 實作 (基於 Playwright)，不需依賴 Node.js。
- 支援開啟使用者電腦中既有的 Edge / Chrome，保留登入狀態。
- 支援在遇到 CAPTCHA 或雙重驗證時，彈出對話框暫停，等待人類親自操作後再繼續。
  **使用情境：**
- 「幫我打開 Google 搜尋今天的台北天氣。」
- 「幫我登入內部系統，然後讀取第一篇文章的內容。」

### 2. 🧠 長期記憶管理 (Memory MCP)

**檔案：** `memory_mcp.py`
**用途：** 讓 AI 能跨對話 (Cross-session) 記住您的個人偏好、專案設定與過往習慣。
**特色：**

- 記憶會永久儲存於本機的 `~/.pocket-agent/memory.json`。
- 具備輕量化的**關鍵字搜尋 (`memory_search`)** 功能，AI 可精準提取所需上下文，大幅節省 Token 消耗。
  **使用情境：**
- 「請幫我記住一件事：這個專案的測試框架一律使用 pytest。」
- (新對話) 「請搜尋我的記憶，看看我習慣用哪個測試框架，然後幫我寫一段程式碼。」

### 3. 🐙 GitHub 整合 (GitHub MCP)

**檔案：** `github_mcp.py`
**用途：** 讓 AI 能夠直接與 GitHub API 互動，獲取 Pull Request、Issue 等協作資訊。
**特色：**

- 透過 `mcp_config.yaml` 的 `inject` 機制注入 `GITHUB_TOKEN`，確保機密金鑰對 LLM 保持隱藏。
  **使用情境：**
- 「請幫我列出 `owner/repo` 狀態為 open 的 PR，並讀取編號 #42 的詳細內容與使用者留言。」
- 「請幫我在 #105 Issue 加上一段回覆：『這個問題我們已經在最新版本修復囉！』」

### 4. 🗄️ 本機資料庫檢索 (Database MCP)

**檔案：** `database_mcp.py`
**用途：** 讓 AI 在開發過程中，能幫您查詢本機開發用的 SQLite 資料庫結構與測試資料。
**特色：**

- 具備**唯讀安全防護**，底層強制限制只能執行字首為 `SELECT` 的查詢語法，不用擔心 AI 誤刪資料。
- 透過 `mcp_config.yaml` 的環境變數 `DB_PATH` 動態指定資料庫位置。
  **使用情境：**
- 「請幫我看看資料庫裡面有哪幾個資料表？」
- 「幫我查詢 `users` 資料表的 Schema 設計，然後用語意化的方式幫我找出前五筆帳號資料。」

---

## 💡 給開發者的注意事項

如果您想要自行開發新的 MCP Server 並放入此目錄：

1. 本專案使用官方 **MCP 2.x SDK**，請務必使用 `from mcp.server.mcpserver import MCPServer`。
2. 開發完成後，請至 `mcp_config.yaml` 中使用 `stdio` 傳輸模式將其掛載。
3. 若您的工具需要金鑰，強烈建議利用 `pocket-agent` 的 `inject` 機制，不要將密碼寫死在程式碼中。
