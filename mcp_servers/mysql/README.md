# MySQL／MariaDB 唯讀檢索與維運診斷 MCP 伺服器（`mysql`）

本套件為 PocketAgent 隨附之遠端 MySQL／MariaDB 結構探查、唯讀查詢、執行計畫分析、結構健康巡檢與維運狀態診斷 MCP 伺服器，透過 `pymysql`（優先）或 `mysql-connector-python` 建立強制唯讀交易連線，支援原始碼開發模式、Portable 免安裝版與 PyInstaller 單一執行檔（`--internal-mcp-server`）。

---

## 模組架構

遵循「共用基礎設施（`_shared/`）＋ 領域子套件（`mysql/`）＋ 頂層薄入口（`mysql_mcp.py`）」三層設計：

| 路徑 | 職責說明 |
| :--- | :--- |
| `mcp_servers/mysql_mcp.py` | Stdio MCP 薄入口，維護啟動契約、`MAX_OUTPUT_CHARS`、`MYSQL_CHECK_TABLE_MAX_BYTES` 與對外匯出清單 |
| `mcp_servers/mysql/server.py` | 透過 `create_mcp_server("mysql", REGISTRARS)` 組裝並註冊工具群組 |
| `mcp_servers/mysql/adapter.py` | 唯讀連線轉接器：憑證快取（10 分鐘 TTL）、TLS／SSL、伺服器端 `MAX_EXECUTION_TIME`、串流游標與快速中止 |
| `mcp_servers/mysql/sql_guard.py` | 具引號狀態機與字面值遮蔽之 `MysqlSqlGuard`（阻擋寫入／鎖表／`LOAD_FILE`／可執行註解，並避免字串常數誤攔） |
| `mcp_servers/mysql/tools/_common.py` | 共用目標解析、`run_readonly_tool` 樣板方法、統一稽核與機密遮罩錯誤封裝 |
| `mcp_servers/mysql/tools/schema.py` | 資料庫／資料表清單、跨庫不混疊的 Schema 檢視與 `SHOW CREATE TABLE` DDL 工具 |
| `mcp_servers/mysql/tools/query.py` | 串流防爆 `SELECT`／`WITH` 查詢與 `EXPLAIN` 執行計畫分析（含自動效能警訊） |
| `mcp_servers/mysql/tools/health.py` | 零鎖表 `INFORMATION_SCHEMA` 健康巡檢（分批限縮與 `BTREE` 冗餘索引偵測）及輕量 `CHECK TABLE` |
| `mcp_servers/mysql/tools/ops.py` | 伺服器狀態摘要、`PROCESSLIST` 活動查詢追蹤與白名單安全複寫狀態巡檢 |

---

## 快速開始與 `mcp_config.yaml` 設定

在專案設定檔 `mcp_config.yaml` 的 `servers:` 區段加入以下設定（唯讀結構與統計工具建議列入 `safe_read_tools`；涉及索引頁讀取鎖的 `mysql_check_table` 與可能含執行中 SQL 片段的 `mysql_inspect_processlist` 建議保留人工確認）：

```yaml
servers:
  mysql:
    transport: stdio
    command: python
    args: ["mcp_servers/mysql_mcp.py"]
    safe_read_tools:
      - mysql_list_databases
      - mysql_list_tables
      - mysql_describe_schema
      - mysql_show_create_table
      - mysql_run_select_query
      - mysql_explain_query
      - mysql_inspect_health
      - mysql_server_status
      - mysql_replication_status
    env:
      MYSQL_HOST: "" # 選填：主機 IP 或位址（留空將於執行時詢問）
      MYSQL_PORT: "3306" # 選填：連接埠，預設 3306
      MYSQL_USER: "" # 選填：登入帳號（留空將於執行時詢問）
      MYSQL_PASSWORD: "" # 選填：登入密碼（建議留空，於執行期安全輸入不落盤）
      MYSQL_DATABASE: "" # 選填：預設資料庫名稱
      MYSQL_CONNECT_TIMEOUT: "10" # 連線逾時秒數（3 ~ 60）
      MYSQL_QUERY_TIMEOUT_MS: "10000" # 伺服器端查詢逾時毫秒數（1000 ~ 60000）
      MYSQL_SSL_MODE: "" # 選填：disabled / preferred / required / verify_ca / verify_identity
      MYSQL_SSL_CA: "" # 選填：TLS CA 憑證檔案路徑
      MYSQL_CHARSET: "utf8mb4" # 選填：連線字元集，預設 utf8mb4
```

### 環境變數說明

| 參數名稱 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `MYSQL_HOST` | `""` | 目標 MySQL／MariaDB 主機 IPv4／IPv6 或主機名稱；留空時於首次呼叫工具互動詢問 |
| `MYSQL_PORT` | `"3306"` | 連接埠（`1` ~ `65535`） |
| `MYSQL_USER` | `""` | 登入帳號 |
| `MYSQL_PASSWORD` | `""` | 登入密碼；留空將於終端機以隱藏輸入詢問並暫存於記憶體（10 分鐘 TTL，認證失敗自動清除） |
| `MYSQL_DATABASE` | `""` | 預設資料庫名稱；留空時可查詢伺服器上所有使用者資料庫與跨庫結構 |
| `MYSQL_CONNECT_TIMEOUT` | `"10"` | TCP 連線與讀寫逾時秒數（夾緊於 `3` ~ `60` 秒） |
| `MYSQL_QUERY_TIMEOUT_MS` | `"10000"` | 伺服器端單次查詢逾時毫秒數（`MAX_EXECUTION_TIME`／MariaDB `max_statement_time`，範圍 `1000` ~ `60000`） |
| `MYSQL_SSL_MODE` | `""` | TLS 連線模式：`disabled`、`preferred`、`required`、`verify_ca`、`verify_identity` |
| `MYSQL_SSL_CA` | `""` | 自訂 CA 憑證路徑（與 `verify_ca`／`verify_identity` 搭配驗證伺服器憑證） |
| `MYSQL_CHARSET` | `"utf8mb4"` | 資料庫連線字元集 |

啟動 PocketAgent 後，可在 CLI 輸入 `/mcp` 確認 `mysql` 已掛載 11 個工具，或輸入 `/mcp mysql_explain_query` 檢視單一工具說明。

---

## 工具清單速查（共 11 項工具）

### 1. 結構探查與 DDL

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `mysql_list_databases` | 是 | 列出目前帳號可見的所有資料庫清單（經 12,000 字元安全截斷） | 無 |
| `mysql_list_tables` | 是 | 列出目標資料庫（或所有使用者資料庫）的基底表與檢視表（`VIEW`） | `database`（選填） |
| `mysql_describe_schema` | 是 | 顯示欄位、型別、主鍵、`NOT NULL`、`DEFAULT`（經遮罩）、`EXTRA`、`COLUMN_COMMENT`、`INDEX_TYPE` 索引與跨庫外鍵（支援多庫同名表分區呈現） | `table_name`（必填）、`database`（選填） |
| `mysql_show_create_table` | 是 | 取得完整 `SHOW CREATE TABLE` DDL 定義（含分區、Generated Columns 與字元集），經遮罩與 12,000 字元截斷 | `table_name`（必填）、`database`（選填） |

### 2. 唯讀查詢與執行計畫

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `mysql_run_select_query` | 是 | 執行單一唯讀 `SELECT`、`WITH` 或 `EXPLAIN` 查詢（Unbuffered 串流防爆、截斷即時中止連線與敏感欄位遮罩） | `query`（必填） |
| `mysql_explain_query` | 是 | 分析純 `SELECT`／`WITH` 的執行計畫（支援 `TRADITIONAL`／`JSON`／`TREE`，禁止 `ANALYZE`），並自動標記 `type=ALL`、`Using filesort`、`Using temporary` 與 `key=NULL` | `query`（必填）、`explain_format`（預設 `"TRADITIONAL"`） |

### 3. 健康巡檢與表完整性檢查

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `mysql_inspect_health` | 是 | 零鎖表 `INFORMATION_SCHEMA` 巡檢：碎片率、無主鍵表、非 InnoDB 引擎、`AUTO_INCREMENT` 溢位水位與 `BTREE` 冗餘前綴索引（限縮前 500 張表分批查詢） | `database`（選填）、`table_name`（選填） |
| `mysql_check_table` | **否（涉及讀取鎖）** | 對單一實體表執行 `CHECK TABLE ... QUICK / FAST`（拒絕 `VIEW` 與超過 2 GB 之大型資料表） | `table_name`（必填）、`database`（選填）、`check_mode`（`QUICK`/`FAST`） |

### 4. 伺服器與維運診斷

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `mysql_server_status` | 是 | 單次輕量查詢版本、Uptime、`read_only`／`super_read_only`、連線數水位、InnoDB Buffer Pool 命中率與慢查詢計數（僅查白名單變數） | 無 |
| `mysql_inspect_processlist` | **否（含 SQL 片段）** | 查詢 `INFORMATION_SCHEMA.PROCESSLIST` 長時間查詢與鎖等待（預設排除 `Sleep` 與本連線，上限 50 筆，`INFO` 經遮罩並限長 200 字元） | `only_active`（預設 `True`）、`min_time_sec`（預設 `0`） |
| `mysql_replication_status` | 是 | 查詢 `SHOW REPLICA STATUS`／`SHOW SLAVE STATUS`，僅採白名單回傳 IO／SQL 執行緒、延遲秒數、錯誤碼與 GTID（嚴格排除帳號與憑證欄位） | 無 |

---

## 實務維運場景與建議提問範例

| 維運情境 | 對應工具 | 建議對 PocketAgent 提問方式 |
| :--- | :--- | :--- |
| **資料庫健檢與索引優化** | `mysql_inspect_health` ＋ `mysql_show_create_table` | 「檢查 `app_db` 的空間碎片、缺少主鍵或 `AUTO_INCREMENT` 快滿的表，並把問題表的 `SHOW CREATE TABLE` 調出來看。」 |
| **慢查詢診斷與索引調校** | `mysql_explain_query` | 「先別跑實際查詢，用 `mysql_explain_query` 分析這段 `JOIN` 查詢有沒有全表掃描或 `Using filesort`。」 |
| **連線暴增／鎖等待排查** | `mysql_server_status` ＋ `mysql_inspect_processlist` | 「看看目前 `Threads_connected` 佔 `max_connections` 幾成，再列出執行超過 5 秒的活動查詢。」 |
| **主從延遲與複寫異常確認** | `mysql_replication_status` | 「檢查這台 Replica 的 IO／SQL 執行緒是否正常、`Seconds_Behind_Source` 延遲多少、有沒有複寫錯誤。」 |
| **安全資料抽樣與彙整** | `mysql_run_select_query` | 「從 `orders` 表依 `status` 分組統計過去 7 天的筆數與金額總和。」 |

---

## 建議操作工作流與安全邊界

1. **交易層與查詢層雙重唯讀**：每次連線立即設定 `SET SESSION TRANSACTION READ ONLY;`、`SET SESSION MAX_EXECUTION_TIME = ...;`（MariaDB 降級 `max_statement_time`）與 `START TRANSACTION READ ONLY;`，且於 `finally` 強制 `rollback()` 與 `close()`，絕不 `commit()`。
2. **字面值感知 SQL 防護與串流熔斷**：`MysqlSqlGuard` 透過引號狀態機移除註解並遮蔽字串字面值骨架，防範 `INTO OUTFILE`、`LOAD_FILE()`、`FOR UPDATE`/`FOR SHARE`、`SLEEP()`、`WAIT_FOR_EXECUTED_GTID_SET()`、可執行註解（`/*!...*/`）與多重語句注入，同時避免對字串內的 `;` 或關鍵字產生誤殺；`SELECT` 採 `SSCursor`／`buffered=False` 分批串流，超過 `12,000` 字元立即截斷並中斷底層連線。
3. **全面遮罩、權限隔離與集中稽核**：敏感欄位（`password`、`token`、`secret` 等）整欄遮罩為 `[REDACTED]`，其餘欄位、`COLUMN_DEFAULT`、`COLUMN_COMMENT`、`PROCESSLIST.INFO` 與錯誤訊息皆經 `DefaultRedactionPolicy` 清洗；複寫狀態嚴格白名單排除來源帳號／主機／憑證欄位，所有操作均寫入 `agent.log` 的 `[AUDIT] [MCP:mysql]` 紀錄。
