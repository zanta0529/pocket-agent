# 瀏覽器自動化 MCP 伺服器（`browser`）

本套件為 PocketAgent 隨附之純 Python Playwright 瀏覽器自動化 MCP 伺服器，透過電腦上既有的 Microsoft Edge／Google Chrome（或既有 CDP 遠端除錯實例）提供受安全邊界約束的網頁瀏覽、元素操作、截圖與人工協作能力，無須執行 `playwright install` 下載瀏覽器二進位檔，支援原始碼開發模式與獨立執行模式（`--internal-mcp-server`；註：`playwright` 為選用依賴，須於環境另行安裝）。

---

## 模組架構

遵循「共用基礎設施（`_shared/`）＋ 領域子套件（`browser/`）＋ 頂層薄入口（`browser_mcp.py`）」三層設計：

| 路徑 | 職責說明 |
| :--- | :--- |
| `mcp_servers/browser_mcp.py` | Stdio MCP 薄入口，維護啟動契約、`MAX_TEXT`／`MAX_OUTPUT_CHARS` 與對外匯出清單 |
| `mcp_servers/browser/server.py` | 透過 `create_mcp_server("browser", ..., lifespan=server_lifespan)` 組裝並註冊工具群組 |
| `mcp_servers/browser/security.py` | `BrowserUrlPolicy` 網址守衛（協定／Userinfo／Metadata／CDP／內網與網域白名單）、`DefaultRedactionPolicy` 遮罩與 `AuditLogger` |
| `mcp_servers/browser/snapshot.py` | DOM 互動元素快照 JS 建構（清除舊 `[data-mcp-ref]`、敏感欄位偵測與元素狀態標記） |
| `mcp_servers/browser/prompt.py` | 跨平台人工確認策略（`UserPromptStrategy`：Win32 `MessageBoxW`／macOS `osascript`／TTY 降級） |
| `mcp_servers/browser/session.py` | `BrowserSessionAdapter` 連線與分頁生命週期管理、Event Loop 鎖、對話框追蹤與截圖目錄解析 |
| `mcp_servers/browser/tools/_common.py` | 共用 `[ref]` 參數驗證、存在性預檢與統一 `[ERROR]` 遮罩封裝 |
| `mcp_servers/browser/tools/navigation.py` | 導覽、返回上一頁與 SPA 條件等待工具 |
| `mcp_servers/browser/tools/inspection.py` | 互動元素快照、可見文字擷取與頁面 PNG 截圖工具 |
| `mcp_servers/browser/tools/actions.py` | 點擊、填表、`<select>` 選取、按鍵、捲動與人工驗證暫停工具 |
| `mcp_servers/browser/tools/tabs.py` | 分頁列舉、切換、關閉與瀏覽器整體資源釋放工具 |

---

## 快速開始與 `mcp_config.yaml` 設定

在專案設定檔 `mcp_config.yaml` 的 `servers:` 區段加入以下設定（僅唯讀檢視與等待類工具建議列入 `safe_read_tools`，其餘導覽與操作類工具維持 Default-Deny 互動確認）：

```yaml
servers:
  browser:
    transport: stdio
    command: python
    args: ["mcp_servers/browser_mcp.py"]
    safe_read_tools:
      - browser_snapshot
      - browser_get_text
      - browser_screenshot
      - browser_wait
      - browser_list_tabs
    env:
      BROWSER_CDP_URL: "" # 選填：如 "http://localhost:9222"，連接既有瀏覽器（留空則自動啟動 Edge/Chrome）
      BROWSER_HEADLESS: "0" # 設為 "1"／"true" 啟用無頭模式
      BROWSER_ALLOWED_DOMAINS: "" # 選填：允許存取之網域白名單（逗號分隔，如 "example.com,*.corp.local"）
      BROWSER_ALLOW_PRIVATE_NETWORKS: "false" # 設為 "true" 允許存取 RFC1918 / ULA 內網私有 IP
      BROWSER_ALLOW_LOOPBACK: "false" # 設為 "true" 允許存取 localhost / 127.0.0.1 / ::1
      BROWSER_TIMEOUT_MS: "15000" # 單次頁面操作逾時毫秒數（3000 ~ 60000）
```

> 若需保留既有登入狀態（例如企業 SSO），可先手動啟動瀏覽器（如 `msedge.exe --remote-debugging-port=9222`），再將 `BROWSER_CDP_URL` 設為 `"http://localhost:9222"`。

### 環境變數說明

| 參數名稱 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `BROWSER_CDP_URL` | `""` | 連接既有 Chromium CDP 遠端除錯端點；留空時依序嘗試啟動系統 Edge、Chrome 或預設 Chromium |
| `BROWSER_HEADLESS` | `"0"` | 設為 `"1"`／`"true"`／`"yes"`／`"on"` 啟用無頭模式（無頭模式下會拒絕呼叫 `browser_wait_for_user`） |
| `BROWSER_ALLOWED_DOMAINS` | `""` | 允許存取之網域白名單（逗號分隔，支援精確網域、`*.domain` 萬用子網域與子網域匹配，自動正規化尾端點號） |
| `BROWSER_ALLOW_PRIVATE_NETWORKS` | `"false"` | 設為 `"true"` 才允許存取 RFC 1918／ULA 內網私有 IP 站點（或將網域顯式列入 `BROWSER_ALLOWED_DOMAINS`） |
| `BROWSER_ALLOW_LOOPBACK` | `"false"` | 設為 `"true"` 才允許存取 `localhost`／`127.0.0.1`／`::1`（仍禁止存取使用中之 CDP 除錯埠） |
| `BROWSER_TIMEOUT_MS` | `"15000"` | Playwright 單次頁面與定位器預設逾時毫秒數（夾緊於 `3000` ~ `60000` 毫秒） |

啟動 PocketAgent 後，可在 CLI 輸入 `/mcp` 確認 `browser` 已掛載 16 個工具，或輸入 `/mcp browser_snapshot` 檢視單一工具說明。

---

## 工具清單速查（共 16 項工具）

### 1. 導覽與等待

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `browser_navigate` | 否 | 正規化並通過網址政策檢查後開啟指定頁面（僅限 `http`／`https`） | `url`（必填） |
| `browser_back` | 否 | 返回瀏覽器上一頁，並於執行前後驗證頁面網址政策 | 無 |
| `browser_wait` | 是 | 等待頁面出現指定文字、網址片段或達到載入狀態（`load`／`domcontentloaded`／`networkidle`） | `text`、`url_contains`、`state`、`timeout_sec`（`1`~`30`） |

### 2. 頁面檢視與截圖

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `browser_snapshot` | 是 | 清除舊 `[data-mcp-ref]` 後為可見互動元素編號，輸出狀態標記（`[disabled]`／`[checked]`／`<select>` 選項）並遮罩敏感欄位 | `selector`（選填 CSS 選擇器） |
| `browser_get_text` | 是 | 擷取 `document.body.innerText` 可見文字，經機密遮罩後安全截斷至 12,000 字元 | 無 |
| `browser_screenshot` | 是 | 擷取目前頁面 PNG 截圖並存於 `~/.pocket-agent/artifacts/browser_shots/`（零工作區污染） | `full_page`（預設 `False`） |

### 3. 頁面互動與人工介入

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `browser_click` | 否 | 點擊指定 `[ref]` 元素，若觸發開啟新分頁會一併提示目前分頁數 | `ref`（必填正整數） |
| `browser_fill` | 否 | 清空並填入指定 `[ref]` 輸入框（`password`、OTP、API Key 等敏感欄位自動遮罩回顯與稽核） | `ref`（必填正整數）、`text`（必填） |
| `browser_select_option` | 否 | 在 `<select>` 下拉選單依 `value` 或 `label` 選取項目 | `ref`（必填正整數）、`value`、`label` |
| `browser_press` | 否 | 發送鍵盤按鍵（例如 `Enter`、`Tab`、`Escape`、`ArrowDown`） | `key`（必填） |
| `browser_scroll` | 否 | 向上或向下捲動頁面（像素受 `100` ~ `3000` 有界限制） | `direction`（`up`／`down`）、`amount`（預設 `600`） |
| `browser_wait_for_user` | 否 | 遇 CAPTCHA、登入或兩步驟驗證時暫停，依 OS 跳出提示等待使用者手動完成 | `message`（選填） |

### 4. 分頁管理與生命週期

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `browser_list_tabs` | 是 | 列出目前所有開啟分頁之 1-based 編號、經遮罩標題與網址（`*` 標記作用中分頁） | 無 |
| `browser_switch_tab` | 否 | 切換至指定編號分頁（切換前強制驗證目標頁面網址是否符合政策） | `index`（必填 1-based 整數） |
| `browser_close_tab` | 否 | 關閉指定分頁（`index=0` 代表目前作用中分頁）並回傳接續頁面狀態 | `index`（預設 `0`） |
| `browser_close` | 否 | 關閉瀏覽器實例與 Playwright driver 並重置內部工作階段狀態 | 無 |

---

## 實務使用場景與建議提問範例

| 使用情境 | 對應工具 | 建議對 PocketAgent 提問方式 |
| :--- | :--- | :--- |
| **閱讀網頁內容與摘要** | `browser_navigate` ＋ `browser_get_text` | 「幫我打開 `https://example.com/docs` 並整理頁面上的重點說明。」 |
| **表單填寫與下拉選單操作** | `browser_snapshot` ＋ `browser_fill` ＋ `browser_select_option` ＋ `browser_click` | 「先掃描表單中的互動元素，填入查詢條件並從地區下拉選單選擇 `Taiwan` 後送出。」 |
| **SPA 動態載入等待與截圖留存** | `browser_wait` ＋ `browser_screenshot` | 「等待畫面上出現 `查詢完成` 後，幫我擷取整頁截圖並回報圖檔路徑。」 |
| **CAPTCHA／登入／雙因素驗證** | `browser_wait_for_user` ＋ `browser_snapshot` | 「遇到登入或驗證碼畫面時請暫停讓我親自操作，我確認完成後再繼續。」 |
| **多開分頁切換與清理** | `browser_list_tabs` ＋ `browser_switch_tab` ＋ `browser_close_tab` | 「列出目前所有開啟的分頁，切換到報表那頁並關掉多餘的分頁。」 |
| **接管已登入的本機瀏覽器** | 設定 `BROWSER_CDP_URL` | 「連上本機 9222 除錯埠的瀏覽器，檢查目前管理後台分頁的狀態。」 |

---

## 建議操作工作流與安全邊界

1. **先快照、再操作；長頁面善用區域聚焦**：互動前務必先呼叫 `browser_snapshot` 取得最新 `[ref]` 編號（每次快照會自動清除殘留的舊 `data-mcp-ref`）。遇到大型後台頁面接近 12,000 字元上限時，傳入 CSS `selector`（例如 `main` 或 `#checkout-form`）可只掃描目標區域的可互動元素。
2. **前置 URL 守衛與 SSRF／CDP 端點隔離**：所有讀寫與互動工具在執行動作或 JS 評估**前**皆會先驗證當前頁面網址（非僅在事後檢查），並於 `context.route` 層級攔截跨站子資源請求；嚴格限制 `http`／`https`、封鎖含 Userinfo 混淆網址（包含 `https://@host`）、永久阻擋雲端 Metadata／Link-Local 位址與使用中之 CDP 除錯埠，違規頁面會立即重置為 `about:blank`。
3. **機密遮罩、零工作區污染與集中稽核**：`browser_snapshot` 與 `browser_fill` 會自動偵測 `password`、`otp`/`totp`、`api_key` 等敏感欄位並以 `********` 遮罩；頁面文字、標題、網址與對話框訊息皆透過 `DefaultRedactionPolicy` 與 `safe_cut_index` 保護，截圖統一存於 `~/.pocket-agent/artifacts/browser_shots/`，所有操作與攔截事件均寫入 `agent.log` 的 `[AUDIT] [MCP:browser]` 紀錄。
4. **跨平台人工確認與刻意不提供的能力**：`browser_wait_for_user` 在 Windows、macOS 與 Linux／無 GUI 環境分別使用原生效用對話框或底層 TTY 互動提示；基於安全設計，本伺服器**不提供**任意 `browser_evaluate` 工具，亦**不支援**自動繞過 CAPTCHA 或反機器人驗證。
