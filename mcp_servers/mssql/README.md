# Microsoft SQL Server (MSSQL) 唯讀檢索與 DMV 診斷 MCP 伺服器（`mssql`）

本套件為 PocketAgent 隨附之遠端 Microsoft SQL Server（T-SQL）結構探查、物件定義檢視、唯讀查詢、純編譯期執行計畫分析、零鎖定 DMV 健康／缺索引巡檢與阻塞／備份／Always On AG 診斷 MCP 伺服器，透過 `pymssql`（優先）或 `pyodbc` 建立唯讀隔離連線，支援原始碼開發模式、Portable 免安裝版與 PyInstaller 單一執行檔（`--internal-mcp-server`）。

---

## 模組架構

遵循「共用基礎設施（`_shared/`）＋ 領域子套件（`mssql/`）＋ 頂層薄入口（`mssql_mcp.py`）」三層設計：

| 路徑 | 職責說明 |
| :--- | :--- |
| `mcp_servers/mssql_mcp.py` | Stdio MCP 薄入口，維護啟動契約、`MAX_OUTPUT_CHARS` 與對外匯出清單 |
| `mcp_servers/mssql/server.py` | 透過 `create_mcp_server("mssql", REGISTRARS)` 組裝並註冊工具群組 |
| `mcp_servers/mssql/adapter.py` | 雙驅動連線轉接器（`pymssql`／`pyodbc`）、`conn.timeout` 與 `SET LOCK_TIMEOUT`、字串感知 `?` 轉換及截斷快速中止 |
| `mcp_servers/mssql/sql_guard.py` | 具巢狀註解／`[...]`／`"..."` 與字面值遮罩之 `MssqlSqlGuard`（封鎖寫入、鎖表提示、`OPENROWSET` 與檔案系統 TVF） |
| `mcp_servers/mssql/tools/_common.py` | 共用 3 段式目標解析、`run_readonly_tool` 樣板方法、權限判斷與遮罩截斷工具 |
| `mcp_servers/mssql/tools/schema.py` | 資料庫／資料表清單、Schema 檢視（含 `INCLUDE` 索引與 `MS_Description`）及 `sys.sql_modules` 物件定義工具 |
| `mcp_servers/mssql/tools/query.py` | 串流防爆 `SELECT`／`WITH` 查詢與 `SET SHOWPLAN_TEXT ON` 純編譯期執行計畫分析工具 |
| `mcp_servers/mssql/tools/health.py` | 零鎖定 DMV 結構健康巡檢（排除 Filtered Index 誤報）與缺索引影響分數排名建議工具 |
| `mcp_servers/mssql/tools/ops.py` | 伺服器屬性狀態、活動請求與阻塞鏈（`blocking_session_id`）及 `msdb` 備份／Always On AG 狀態工具 |

---

## 快速開始與 `mcp_config.yaml` 設定

在專案設定檔 `mcp_config.yaml` 的 `servers:` 區段加入以下設定（唯讀結構、統計與 DMV 建議工具可列入 `safe_read_tools`；可能包含執行中 SQL 片段的 `mssql_inspect_active_requests` 建議保留人工互動確認）：

```yaml
servers:
  mssql:
    transport: stdio
    command: python
    args: ["mcp_servers/mssql_mcp.py"]
    safe_read_tools:
      - mssql_list_databases
      - mssql_list_tables
      - mssql_describe_schema
      - mssql_get_object_definition
      - mssql_run_select_query
      - mssql_explain_query
      - mssql_inspect_health
      - mssql_inspect_missing_indexes
      - mssql_server_status
      - mssql_backup_and_ag_status
    env:
      MSSQL_HOST: "" # 選填：主機 IP 或 host\instance（留空將於執行時詢問）
      MSSQL_PORT: "1433" # 選填：連接埠，預設 1433
      MSSQL_USER: "" # 選填：登入帳號（留空將於執行時詢問）
      MSSQL_PASSWORD: "" # 選填：登入密碼（建議留空，於執行期安全輸入不落盤）
      MSSQL_DATABASE: "" # 選填：預設資料庫名稱
      MSSQL_CONNECT_TIMEOUT: "10" # 登入連線逾時秒數（3 ~ 60）
      MSSQL_QUERY_TIMEOUT_SEC: "15" # 查詢執行逾時秒數（3 ~ 120）
      MSSQL_LOCK_TIMEOUT_MS: "5000" # 鎖等待逾時毫秒數（1000 ~ 30000）
      MSSQL_DRIVER: "" # 選填：指定 pyodbc 使用之 ODBC 驅動名稱
      MSSQL_ENCRYPT: "yes" # pyodbc TLS 加密（yes / no）
      MSSQL_TRUST_SERVER_CERTIFICATE: "yes" # 測試／內網環境預設信任憑證；正式嚴格驗證請設為 no
```

### 環境變數說明

| 參數名稱 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `MSSQL_HOST` | `""` | 目標 SQL Server 主機 IPv4／IPv6、主機名稱或 `host\instance`；留空於首次呼叫工具詢問 |
| `MSSQL_PORT` | `"1433"` | 連接埠（`1` ~ `65535`） |
| `MSSQL_USER` | `""` | 登入帳號（支援 `User`、`Domain\User` 與 `User@Domain`） |
| `MSSQL_PASSWORD` | `""` | 登入密碼；留空將於終端機以隱藏輸入詢問並暫存於記憶體（10 分鐘 TTL，認證失敗自動清除） |
| `MSSQL_DATABASE` | `""` | 預設資料庫名稱；支援於查詢或結構工具使用 `db.schema.table` 三段式指定 |
| `MSSQL_CONNECT_TIMEOUT` | `"10"` | 登入連線階段逾時秒數（夾緊於 `3` ~ `60` 秒） |
| `MSSQL_QUERY_TIMEOUT_SEC` | `"15"` | 單次查詢執行逾時秒數（`pymssql timeout` 與 `pyodbc conn.timeout`，範圍 `3` ~ `120` 秒） |
| `MSSQL_LOCK_TIMEOUT_MS` | `"5000"` | `SET LOCK_TIMEOUT` 鎖等待逾時毫秒數（範圍 `1000` ~ `30000` 毫秒） |
| `MSSQL_DRIVER` | `""` | 指定 `pyodbc` 降級模式之 ODBC Driver（留空自動偵測 ODBC Driver 18/17） |
| `MSSQL_ENCRYPT` | `"yes"` | `pyodbc` 連線字串之 `Encrypt` 屬性（`yes`／`no`） |
| `MSSQL_TRUST_SERVER_CERTIFICATE` | `"yes"` | `pyodbc` 連線字串之 `TrustServerCertificate` 屬性（正式環境建議設為 `"no"` 驗證伺服器憑證） |

啟動 PocketAgent 後，可在 CLI 輸入 `/mcp` 確認 `mssql` 已掛載 11 個工具，或輸入 `/mcp mssql_explain_query` 檢視單一工具說明。

---

## 工具清單速查（共 11 項工具）

### 1. 結構探查與物件定義

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `mssql_list_databases` | 是 | 列出所有狀態為 `ONLINE` 的資料庫清單（經 12,000 字元安全截斷） | 無 |
| `mssql_list_tables` | 是 | 列出目標資料庫的基底表與檢視表（`VIEW`）清單（支援跨庫指定） | `database`（選填） |
| `mssql_describe_schema` | 是 | 顯示欄位、型別精度、主鍵、`DEFAULT`（遮罩）、`IDENTITY`／`COMPUTED`、`MS_Description`、`INCLUDE`／`WHERE` 索引與跨 Schema 外鍵 | `table_name`（必填）、`database`（選填） |
| `mssql_get_object_definition` | 是 | 取得 View／Stored Procedure／Function／Trigger 的完整 T-SQL 定義（`sys.sql_modules`）或資料表結構摘要 | `object_name`（必填）、`database`（選填） |

### 2. 唯讀查詢與執行計畫

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `mssql_run_select_query` | 是 | 執行單一唯讀 `SELECT`／`WITH` 查詢（串流防爆、截斷即呼叫 `cursor.cancel()` 中止與機密欄位遮罩） | `query`（必填） |
| `mssql_explain_query` | 是 | 以 `SET SHOWPLAN_TEXT ON` 取得純編譯期預估計畫（絕不實際執行查詢），自動標記 `Table Scan`、`Key Lookup` 與 `Sort` 警訊 | `query`（必填） |

### 3. 健康巡檢與缺索引建議

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `mssql_inspect_health` | 是 | 零鎖定 DMV 巡檢：Heap 表、缺少主鍵、未用保留空間、停用索引、未受信任約束、Identity 溢位水位與非篩選 B-Tree 冗餘索引（無權限自動降級） | `database`（選填）、`table_name`（選填） |
| `mssql_inspect_missing_indexes` | 是 | 查詢 `sys.dm_db_missing_index_*` DMV，依預估影響分數排序列出 `equality`／`inequality`／`include` 缺索引建議 | `database`（選填）、`top_n`（`1`~`50`，預設 `20`） |

### 4. 伺服器與維運診斷

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `mssql_server_status` | 是 | 查詢 `@@VERSION`、`SERVERPROPERTY`、HADR 旗標、啟動時間、CPU／實體記憶體與使用者工作階段數（無 DMV 權限自動降級） | 無 |
| `mssql_inspect_active_requests` | **否（含 SQL 片段）** | 查詢活動請求與阻塞鏈（`blocking_session_id`）、等待類型與耗時（`TOP 50`，SQL 經遮罩並限長 200 字元） | `min_elapsed_ms`（預設 `0`）、`blocking_only`（預設 `False`） |
| `mssql_backup_and_ag_status` | 是 | 檢查線上資料庫最近 `FULL`／`DIFF`／`LOG` 備份（逾 7 天或無完整備份自動警示）與 Always On AG 同步／佇列狀態 | `database`（選填） |

---

## 實務維運場景與建議提問範例

| 維運情境 | 對應工具 | 建議對 PocketAgent 提問方式 |
| :--- | :--- | :--- |
| **阻塞與死結前兆排查** | `mssql_inspect_active_requests` | 「目前資料庫回應變慢，幫我列出正在執行或發生阻塞（`blocking_only=True`）的請求與等待類型。」 |
| **慢查詢調校與缺索引盤點** | `mssql_explain_query` ＋ `mssql_inspect_missing_indexes` | 「用 `mssql_explain_query` 看這段報表 SQL 有沒有 `Key Lookup` 或 `Table Scan`，並查一下 `erp_db` 前 10 個缺索引建議。」 |
| **資料庫體質與約束健檢** | `mssql_inspect_health` ＋ `mssql_get_object_definition` | 「巡檢 `erp_db` 有沒有 Heap 表、停用索引或未受信任外鍵，並幫我調出 `dbo.usp_SyncOrders` 的預存程序內容。」 |
| **備份合規與 Always On AG 巡檢** | `mssql_backup_and_ag_status` | 「確認所有線上資料庫最近一次完整備份時間有沒有超過 7 天，並檢查 Always On AG 的同步與 Send／Redo 佇列。」 |

---

## 建議操作工作流與安全邊界

1. **唯讀隔離、雙驅動逾時與鎖等待熔斷**：連線後強制設定 `ApplicationIntent=ReadOnly`（`pyodbc`）、`conn.timeout = query_timeout`（修復 `pyodbc` 查詢無限等待）、`SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;` 與 `SET LOCK_TIMEOUT`（防範遭線上 DDL `Sch-M` 鎖卡死）；大查詢截斷時呼叫 `cursor.cancel()` 直接中斷連線，絕不 `commit()`。
2. **T-SQL 雙軌字面值遮罩與防鎖／防探測**：`MssqlSqlGuard` 支援巢狀 `/* */` 註解驗證、`[...]`／`"..."` 識別字與字串常值遮蔽骨架，阻擋 `EXEC`／`OPENROWSET`／`INTO`／`WAITFOR`、資料表強鎖提示（`UPDLOCK`、`XLOCK`、`TABLOCKX`、`HOLDLOCK`）與檔案系統 TVF（`dm_os_enumerate_filesystem`、`fn_get_audit_file`），同時避免字串內的 `;` 或關鍵字遭誤攔。
3. **全面遮罩與權限優雅降級**：敏感欄位整欄遮罩為 `[REDACTED]`，`COLUMN_DEFAULT`、`MS_Description`、T-SQL 定義、活動查詢片段與錯誤訊息皆經 `DefaultRedactionPolicy` 清洗；缺少 `VIEW DATABASE STATE`、`VIEW SERVER STATE` 或 `msdb` 權限時精準降級或友善回報（不清除已快取憑證），所有操作皆寫入 `agent.log` 的 `[AUDIT] [MCP:mssql]` 紀錄。
