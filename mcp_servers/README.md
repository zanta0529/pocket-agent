# Pocket Agent MCP 工具擴充指南

本目錄（`mcp_servers/`）包含專為 Pocket Agent 設計的 Model Context Protocol（MCP）Stdio 伺服器。透過這些擴充，AI 代理可在受控的安全邊界內與本機環境、資料庫、遠端主機、GitHub 或瀏覽器互動。

## 如何啟用 MCP 伺服器

Pocket Agent 透過專案根目錄（或使用者目錄）的 `mcp_config.yaml` 決定掛載哪些 MCP 伺服器。
請參考 `mcp_config.example.yaml`，將需要的服務設定複製過去並調整環境變數或參數，再啟動 Pocket Agent 即可。

各伺服器區塊皆支援 `enabled: true`／`enabled: false`（省略時預設為 `true`），可快速切換是否載入，無須反覆註解整段設定：

```yaml
servers:
  github:
    enabled: false # 暫時停用：啟動時不載入，但保留下方設定
    transport: stdio
    command: python
    args: ['mcp_servers/github_mcp.py']
```

---

## 目錄架構與設計原則

本目錄採用「共用基礎設施（`_shared/`）＋領域子套件（`<domain>/`）＋薄入口（`<name>_mcp.py`）」的模組化分層設計，同時維持 `mcp_config.yaml` 與 `--internal-mcp-server`（`runpy.run_path`）的直接啟動能力：

```text
mcp_servers/
├── _shared/              # 跨伺服器共用基礎設施（bootstrap、constants、i18n、遮罩、稽核、TTY、憑證快取、DB 基底）
├── browser/              # 瀏覽器自動化領域子套件（security、snapshot、prompt、session、server、tools/）
├── sqlite/               # SQLite 領域子套件（sql_guard、adapter、server、tools/）
├── mysql/                # MySQL / MariaDB 領域子套件（sql_guard、adapter、server、tools/）
├── mssql/                # Microsoft SQL Server 領域子套件（sql_guard、adapter、server、tools/）
├── github/               # GitHub 領域子套件（security、client、templates、server、tools/）
├── rhel/                 # RHEL SSH 領域子套件（security、ssh_client、server、tools/）
├── win_rdp/              # Windows RDP 領域子套件（security、win32_api、session、server、tools/）
├── netdiag/              # 網站與網路舉證診斷領域子套件（policy、redaction、dns_adapter、tls_probe、http_probe、pipeline、report、tools/）
├── browser_mcp.py        # 薄入口（對應 browser/）
├── sqlite_mcp.py         # 薄入口（對應 sqlite/）
├── mysql_mcp.py          # 薄入口（對應 mysql/）
├── mssql_mcp.py          # 薄入口（對應 mssql/）
├── github_mcp.py         # 薄入口（對應 github/）
├── rhel_mcp.py           # 薄入口（對應 rhel/）
├── win_rdp_mcp.py        # 薄入口（對應 win_rdp/）
├── netdiag_mcp.py        # 薄入口（對應 netdiag/）
└── memory_mcp.py         # 輕量單檔伺服器（本機 JSON 長期記憶）
```

### 核心設計模式

- **Factory（工廠）**：透過 `mcp_servers._shared.create_mcp_server(name, registrars)` 統一建立 `MCPServer` 實例並依序掛載工具群組。
- **Command / Registrar（指令註冊）**：各領域在 `tools/*.py` 依功能切分工具群組，每個模組提供 `register(mcp)` 函式，新增工具時無須修改既有工具檔案。
- **Adapter（轉接器）**：資料庫（`SqliteAdapter`、`MysqlAdapter`、`MssqlAdapter`）、外部 API（`GitHubClient`）、遠端傳輸（`rhel/ssh_client.py`、`win_rdp/win32_api.py`、`win_rdp/session.py`）將底層協定差異封裝於獨立層，與工具介面解耦。
- **Strategy（策略）**：資料庫 SQL 唯讀守門員（`SqlGuard` + 各方言策略）、稽核策略與輸出遮罩規則皆集中於共用層或領域 `security.py`／`sql_guard.py`。
- **i18n 相容**：統一由 `mcp_servers._shared.t(key, **kwargs)` 代理 `pocket_agent.system.i18n.t`，獨立執行缺少主套件時自動降級為純鍵值回傳，不硬編碼備援文案。

---

## 目前提供的 MCP 伺服器清單（共 9 座）

### 1. 瀏覽器自動化（Browser MCP）

- **入口**：`browser_mcp.py` → `mcp_servers/browser/`
- **用途**：基於 Playwright 操作本機 Edge／Chrome（無須 `playwright install`），支援導覽、等待、CSS 區域快照、截圖（集中存於 `~/.pocket-agent/artifacts/browser_shots/`）、點擊、填表、`<select>` 下拉選取、多分頁切換／關閉、跨平台人工驗證暫停，以及協定／內網／網域白名單守衛與機密遮罩稽核。

### 2. 長期記憶管理（Memory MCP）

- **入口**：`memory_mcp.py`（輕量單檔）
- **用途**：將個人偏好、專案慣例與跨對話筆記儲存於 `~/.pocket-agent/memory.json`，提供關鍵字檢索、更新與刪除。

### 3. GitHub 整合（GitHub MCP）

- **入口**：`github_mcp.py` → `mcp_servers/github/`
- **用途**：檢索與更新 Issue／PR、讀取與提交行內 Review 意見、診斷 CI Check Annotations 與 Actions Workflow Runs、唯讀探索儲存庫檔案與分支／Tag／Release，並支援 GHES 與範本感知 PR 建立；透過 `inject` 隱藏 `GITHUB_TOKEN`，不提供自動 Merge 與 `APPROVE` 工具。

### 4. 本機 SQLite 資料庫（SQLite MCP）

- **入口**：`sqlite_mcp.py` → `mcp_servers/sqlite/`
- **用途**：本機 SQLite 物件／Schema／DDL 探查（含產生欄、遮罩預設值與 Partial 索引）、雙軌狀態機防注入之唯讀 `SELECT` 與 `EXPLAIN QUERY PLAN` 計畫分析、資料庫資訊與 `dbstat` 空間統計、唯讀健康／完整性巡檢（Authorizer + `ro` URI + `progress_handler` 逾時三重防護，並受 500 MB 熔斷保護）。

### 5. 遠端 MySQL／MariaDB 資料庫（MySQL MCP）

- **入口**：`mysql_mcp.py` → `mcp_servers/mysql/`
- **用途**：透過 `pymysql`／`mysql-connector-python`（支援 TLS／SSL 與 `MAX_EXECUTION_TIME` 查詢逾時）提供跨庫不混疊結構探查與 `SHOW CREATE TABLE`、字面值感知防注入之串流 `SELECT` 與 `EXPLAIN` 計畫分析、零鎖表健康巡檢、單表 `CHECK TABLE`（2 GB 熔斷）及伺服器狀態／`PROCESSLIST`／白名單複寫狀態診斷。

### 6. 遠端 Microsoft SQL Server 資料庫（MSSQL MCP）

- **入口**：`mssql_mcp.py` → `mcp_servers/mssql/`
- **用途**：透過 `pymssql`／`pyodbc`（含 `conn.timeout` 與 `SET LOCK_TIMEOUT`）提供結構探查與 `sys.sql_modules` 物件定義、雙軌字面值遮罩防注入之 `SELECT`／`WITH` 與 `SHOWPLAN_TEXT` 計畫分析、零鎖定 DMV 健康／缺索引巡檢，以及伺服器狀態、阻塞請求鏈與備份／Always On AG 診斷。

### 7. 遠端 RHEL 主機維運巡檢（RHEL MCP）

- **入口**：`rhel_mcp.py` → `mcp_servers/rhel/`
- **用途**：透過系統 OpenSSH（支援指定私鑰與 `SSH_ASKPASS` 密碼驗證）執行 12 項結構化唯讀巡檢（系統概況、儲存、網路、服務、日誌、SELinux／防火牆與設定檔安全讀取）。

### 8. 遠端 Windows 主機 RDP 操控與巡檢（Win RDP MCP）

- **入口**：`win_rdp_mcp.py` → `mcp_servers/win_rdp/`
- **用途**：透過 Port 3389 與系統可信 `mstsc.exe` 提供 Layer A 視覺截圖與前景保護鍵鼠操作，以及 Layer B 剪貼簿 Enveloped Sentinel 唯讀巡檢（自動備份與還原剪貼簿）。

### 9. 網站與網路跨單位舉證診斷（NetDiag MCP）

- **入口**：`netdiag_mcp.py` → `mcp_servers/netdiag/`
- **用途**：預設停用（`enabled: false`）且強制逐次人工確認，提供階段式 `DNS → TCP → TLS 憑證鏈 → HTTP` 唯讀診斷與多解析器 DNS 比對，支援公開 HTTP／HTTPS 與受控內網私有 IP 站點（含 `host_header` SNI／Host 指定、雙軌 CA 對照與過期憑證公開欄位擷取）。

---

## 開發新工具或新 MCP 伺服器指南

1. **擴充既有伺服器的工具**：
   - 在對應領域的 `mcp_servers/<domain>/tools/` 新增函式（或新增子模組），於該模組的 `register(mcp)` 註冊，並在 `tools/__init__.py` 加入 `REGISTRARS`。
   - 若需要讓測試或外部直接由薄入口匯入，在 `mcp_servers/<name>_mcp.py` 補上 re-export。
2. **新增獨立 MCP 伺服器**：
   - 依複雜度選擇單檔（工具數少、無複雜安全層）或子套件結構（`security.py` + 傳輸／轉接層 + `tools/` + `server.py` + 薄入口）。
   - 使用官方 **MCP 2.x SDK**，並優先透過 `mcp_servers._shared.create_mcp_server`、`CONSTANTS`、`CredentialCache`、`tty_prompt` 與 `t` 複用共用設施。
   - 敏感憑證請透過 `mcp_config.yaml` 的 `inject` 或執行期互動提示（`CONN_PARAM_SPECS`）傳遞，切勿硬編碼於程式碼中。
   - 僅供 MCP 使用的第三方依賴（例如 `pymysql`、`pymssql`、`playwright`）應限制於 `mcp_servers/` 內匯入，不得流入 `src/pocket_agent/` 核心套件。
