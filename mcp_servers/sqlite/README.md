# 本機 SQLite 資料庫唯讀檢索與維運診斷 MCP 伺服器（`sqlite`）

本套件為 PocketAgent 隨附之本機 SQLite 資料庫結構探查、DDL 定義檢視、唯讀查詢、執行計畫分析、健康／完整性巡檢與空間統計 MCP 伺服器，透過 `file:?mode=ro` URI、`sqlite3.set_authorizer` 與 `set_progress_handler` 建立雙重唯讀隔離與逾時保護，支援原始碼開發模式、Portable 免安裝版與 PyInstaller 單一執行檔（`--internal-mcp-server`）。

---

## 模組架構

遵循「共用基礎設施（`_shared/`）＋ 領域子套件（`sqlite/`）＋ 頂層薄入口（`sqlite_mcp.py`）」三層設計：

| 路徑 | 職責說明 |
| :--- | :--- |
| `mcp_servers/sqlite_mcp.py` | Stdio MCP 薄入口，維護啟動契約、`MAX_OUTPUT_CHARS`、`SQLITE_QUICK_CHECK_MAX_BYTES` 與對外匯出清單 |
| `mcp_servers/sqlite/server.py` | 透過 `create_mcp_server("sqlite", REGISTRARS)` 組裝並註冊工具群組 |
| `mcp_servers/sqlite/adapter.py` | 唯讀 URI 連線轉接器、UNC／混合斜線／網路符號連結阻斷、Authorizer（PRAGMA 與函式黑名單）及 `progress_handler` 查詢逾時中斷 |
| `mcp_servers/sqlite/sql_guard.py` | 具引號／方括號／註解狀態機與字面值遮蔽之 `SqliteSqlGuard`（單一唯讀 `SELECT`／`WITH`／`EXPLAIN` 驗證） |
| `mcp_servers/sqlite/tools/_common.py` | 共用物件名稱驗證、識別字雙引號跳脫、虛擬表／Shadow Table 辨識、`run_readonly_tool` 樣板方法與遮罩截斷工具 |
| `mcp_servers/sqlite/tools/schema.py` | 物件清單篩選、強化 Schema 檢視（`table_xinfo` 產生欄、遮罩預設值、複合主鍵順位與外鍵動作）及 `sqlite_get_ddl` |
| `mcp_servers/sqlite/tools/query.py` | 串流防爆 `SELECT`／`WITH` 查詢、`EXPLAIN QUERY PLAN` 樹狀分析與 `sqlite_switch_database` 安全切換工具 |
| `mcp_servers/sqlite/tools/health.py` | 空間／WAL／碎片／無主鍵／精準冗餘索引／外鍵健康巡檢與 `sqlite_check_integrity`（受 500 MB 熔斷保護） |
| `mcp_servers/sqlite/tools/ops.py` | 引擎版本／編譯能力／PRAGMA 中繼資訊（`sqlite_database_info`）與 `dbstat`／`sqlite_stat1` 空間統計（`sqlite_table_stats`） |

---

## 快速開始與 `mcp_config.yaml` 設定

在專案設定檔 `mcp_config.yaml` 的 `servers:` 區段加入以下設定（唯讀結構、查詢與統計工具可列入 `safe_read_tools`；涉及較重磁碟 I/O 掃描的 `sqlite_check_integrity` 建議保留人工互動確認）：

```yaml
servers:
  sqlite:
    transport: stdio
    command: python
    args: ["mcp_servers/sqlite_mcp.py"]
    safe_read_tools:
      - sqlite_list_tables
      - sqlite_describe_schema
      - sqlite_get_ddl
      - sqlite_run_select_query
      - sqlite_explain_query
      - sqlite_inspect_health
      - sqlite_database_info
      - sqlite_table_stats
      - sqlite_switch_database
    env:
      DB_PATH: "dev.db" # 選填：SQLite 資料庫檔案路徑（留空將於執行時詢問）
      SQLITE_QUERY_TIMEOUT_SEC: "15" # 選填：單次查詢 progress_handler 逾時秒數（1 ~ 120）
      SQLITE_BUSY_TIMEOUT_SEC: "5" # 選填：唯讀連線鎖等待逾時秒數（1 ~ 30）
```

### 環境變數說明

| 參數名稱 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `DB_PATH` | `""` | 目標 SQLite 資料庫檔案的絕對或相對路徑；留空或省略時於首次呼叫工具時互動詢問並快取於記憶體（10 分鐘 TTL） |
| `SQLITE_QUERY_TIMEOUT_SEC` | `"15"` | 單次查詢執行逾時秒數（透過 `conn.set_progress_handler` 中斷，範圍 `1` ~ `120` 秒），防範遞迴 CTE 或笛卡兒積 DoS |
| `SQLITE_BUSY_TIMEOUT_SEC` | `"5"` | 開啟唯讀連線時的鎖等待逾時秒數（範圍 `1` ~ `30` 秒） |

啟動 PocketAgent 後，可在 CLI 輸入 `/mcp` 確認 `sqlite` 已掛載 10 個工具，或輸入 `/mcp sqlite <資料庫路徑>` 直接於對話中切換連線目標。

---

## 工具清單速查（共 10 項工具）

### 1. 結構探查與 DDL 定義

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `sqlite_list_tables` | 是 | 列出資料庫物件清單（預設表與檢視表，支援篩選 `all`／`table`／`view`／`index`／`trigger` 與名稱模糊比對） | `object_type`（選填）、`name_pattern`（選填） |
| `sqlite_describe_schema` | 是 | 顯示欄位、產生欄（`GENERATED`）、遮罩 `DEFAULT`、複合主鍵順位、Partial／Expression 索引與外鍵 `ON DELETE/UPDATE` | `table_name`（必填） |
| `sqlite_get_ddl` | 是 | 唯讀取得指定 Table（含附屬 Index／Trigger）、View、Index 或 Trigger 之原始 `CREATE` DDL 定義 | `object_name`（必填）、`object_type`（選填）、`include_related`（預設 `True`） |

### 2. 唯讀查詢、執行計畫與切換

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `sqlite_run_select_query` | 是 | 執行單一唯讀 `SELECT`／`WITH`／`EXPLAIN` 查詢（串流防爆、截斷即中止、逾時熔斷與機密欄位遮罩） | `query`（必填） |
| `sqlite_explain_query` | 是 | 執行 `EXPLAIN QUERY PLAN` 並以縮排樹狀呈現計畫，自動標記 `SCAN`、`USE TEMP B-TREE` 與 `CORRELATED` 警訊 | `query`（必填） |
| `sqlite_switch_database` | 是（路徑動態授權） | 於對話中切換目前連線的 SQLite 資料庫；工作區內路徑免確認，工作區外／敏感目錄仍須確認，UNC 路徑一律拒絕 | `target_db_path`（必填） |

### 3. 健康巡檢與完整性檢查

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `sqlite_inspect_health` | 是 | 檢查主檔與 WAL 容量、Freelist 碎片率、缺少主鍵（排除虛擬表）、非 Partial 冗餘前綴索引、外鍵孤兒與受 500 MB 熔斷保護之 `quick_check` | `table_name`（選填）、`include_quick_check`（預設 `True`） |
| `sqlite_check_integrity` | **否（較重磁碟 I/O）** | 執行 `PRAGMA quick_check`（`quick`）或 `PRAGMA integrity_check`（`full`，含索引內容一致性），支援單表與外鍵驗證（受 500 MB 熔斷保護） | `table_name`（選填）、`check_mode`（`quick`/`full`）、`include_fk_check`（預設 `True`） |

### 4. 中繼狀態與空間統計

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `sqlite_database_info` | 是 | 查詢 SQLite 版本、編譯能力白名單（`FTS5`／`JSON1`／`RTREE`／`DBSTAT`）、主檔／WAL／SHM 大小、PRAGMA 設定與各類物件統計 | 無 |
| `sqlite_table_stats` | 是 | 統計各表與索引實體頁數、實佔容量與未用空間 Top N（優先使用 `dbstat`，不支援時優雅降級至 `sqlite_stat1`／結構摘要，絕不執行全表 `COUNT(*)`） | `top_n`（`1`~`50`，預設 `20`） |

---

## 安全防禦邊界

1. **URI `mode=ro`、Authorizer 與 `progress_handler` 三重唯讀防線**：連線以 `?mode=ro` 開啟，底層掛載 `sqlite3.set_authorizer` 阻斷所有寫入與管理操作、禁止帶值變更設定型 PRAGMA，並封鎖 `load_extension`、`readfile`、`writefile`、`edit`、`fts3_tokenizer`、`zeroblob`、`randomblob` 等危險／記憶體炸彈函式；查詢執行期間透過 `set_progress_handler` 強制執行秒級逾時熔斷。
2. **雙軌引號狀態機守衛**：`SqliteSqlGuard` 正確區分單／雙引號、反引號、方括號識別字與註解，確保條件字串內含 `;` 或 `'UPDATE'` 不會誤攔，同時嚴格阻絕多重語句、未閉合引號／註解與非唯讀關鍵字。
3. **UNC／混合斜線／網路符號連結防線與快取隔離**：先於任何 `Path.resolve()` 前攔截 `\\`、`//`、`/\`、`\/`、`\\?\`、`\\.\`、NUL 字元與指向網路位置之符號連結，杜絕 Windows SMB NetNTLMv2 認證雜湊外洩；單次查詢傳入 `db_path` 不會靜默覆寫全域快取或清空原正常連線。
4. **全面遮罩與稽核軌跡**：查詢敏感欄位整欄遮罩為 `[REDACTED]`，Schema `DEFAULT` 預設值、DDL、健康報告與錯誤訊息皆經 `DefaultRedactionPolicy` 清洗與 12,000 字元截斷，所有存取皆寫入 `agent.log` 的 `[AUDIT] [MCP:sqlite]` 紀錄。
