# Pocket Agent 設定檔規格說明書 (Configuration Specification)

本文件完整列出 Pocket Agent 所支援的所有設定檔鍵值、資料型態、預設行為、環境變數映射與自訂範例。

---

## 1. 設定檔架構與載入順序

Pocket Agent 採用階層式配置（Layered Configuration），載入優先順序如下（由高至低）：

1. **CLI 執行期即時指令**（例如 `/model <provider> <model>`、`/config set <key> <value>`）
2. **環境變數 (Environment Variables)**（定義於系統環境或本機 `.env` 檔案中）
3. **使用者全域設定檔 (`config.json`)**：
   - Windows：`%USERPROFILE%\.pocket-agent\config.json`（例如 `C:\Users\<username>\.pocket-agent\config.json`）
   - Linux / macOS：`~/.pocket-agent/config.json`
4. **系統預設值 (`DEFAULT_CONFIG`)**

---

## 2. 完整配置鍵值參考表

### 2.1 頂層設定 (Top-level Settings)

| 鍵值 (Key)        | 型別     | 預設值          | 環境變數                | 說明                                                                                                           |
| :---------------- | :------- | :-------------- | :---------------------- | :------------------------------------------------------------------------------------------------------------- |
| `active_provider` | `string` | `"openai"`      | `POCKET_AGENT_PROVIDER` | 目前啟用的主要模型供應商（如 `openai`, `openrouter`, `ollama`, `ollama_cloud`, `sambanova`, `groq`, `gemini`） |
| `active_model`    | `string` | `"gpt-6-astra"` | `OPENAI_MODEL`          | 目前啟用的模型名稱                                                                                             |

---

### 2.2 系統與核心行為設定 (`system`)

```json
{
  "system": {
    "max_tool_rounds": 20,
    "log_level": "WARNING",
    "use_sandbox": "none",
    "sandbox_container": "pocket_sandbox",
    "auto_learn": false,
    "context_window": null,
    "context_reserve_output": 4096,
    "context_safety_margin": 1024,
    "context_compact_target_ratio": 0.6,
    "spillover_threshold_chars": null,
    "spillover_head_chars": 1500,
    "spillover_tail_chars": 1500,
    "repo_map_budget": 1024,
    "housekeeping_enabled": true,
    "housekeeping_ttl_days": 7,
    "housekeeping_max_files": 100,
    "housekeeping_max_size_mb": 50,
    "auto_update": true,
    "update_repo": ""
  }
}
```

| 鍵值 (Key)                 | 型別      | 預設值             | 環境變數                  | 說明                                                                       |
| :------------------------- | :-------- | :----------------- | :------------------------ | :------------------------------------------------------------------------- |
| `system.max_tool_rounds`   | `integer` | `20`               | `MAX_TOOL_ROUNDS`         | 單輪對話中允許 Agent 連續調用工具的最大輪次上限。                          |
| `system.log_level`         | `string`  | `"WARNING"`        | `POCKET_AGENT_LOG_LEVEL`  | 終端機與檔案的日誌記錄等級（可設 `DEBUG`, `INFO`, `WARNING`, `ERROR`）。檔案儲存於全域 `~/.pocket-agent/workspaces/<slug>/logs/agent.log`。 |
| `system.use_sandbox`       | `string`  | `"none"`           | `USE_SANDBOX`             | 命令列執行沙盒模式（`none` 表示直接本機執行；`docker` 表示透過容器隔離）。 |
| `system.sandbox_container` | `string`  | `"pocket_sandbox"` | `SANDBOX_CONTAINER`       | 沙盒 Docker 容器名稱。                                                     |
| `system.auto_learn`        | `boolean` | `false`            | `POCKET_AGENT_AUTO_LEARN` | 是否在對話結束後自動分析萃取共用技能。                                     |

#### 上下文預算控制 (Context Budget)

| 鍵值 (Key)                            | 型別                | 預設值 | 說明                                                                                       |
| :------------------------------------ | :------------------ | :----- | :----------------------------------------------------------------------------------------- |
| `system.context_window`               | `integer` \| `null` | `null` | 模型 Context 視窗總上限 Tokens。設為 `null` 時由引擎自動向端點探測或套用保底值 (128,000)。 |
| `system.context_reserve_output`       | `integer`           | `4096` | 預留給模型回答與生成內容的空間 Tokens。                                                    |
| `system.context_safety_margin`        | `integer`           | `1024` | 避免臨界抖動的系統防禦緩衝區 Tokens。                                                      |
| `system.context_compact_target_ratio` | `float`             | `0.6`  | 歷史對話壓縮與清理舊工具輸出時的目標水位比例（預留 40% 成長空間）。                        |

#### 大工具輸出落盤虛擬化 (Spill-to-Disk)

| 鍵值 (Key)                         | 型別                | 預設值 | 環境變數                           | 說明                                                                                             |
| :--------------------------------- | :------------------ | :----- | :--------------------------------- | :----------------------------------------------------------------------------------------------- |
| `system.spillover_threshold_chars` | `integer` \| `null` | `null` | `POCKET_AGENT_SPILLOVER_THRESHOLD` | 啟動工具巨量輸出落盤的字元門檻。設為 `null` 時動態依模型輸入預算之 15% 計算（保底 6,000 字元）。儲存於工作區沙盒 `scratch/outputs/`。 |
| `system.spillover_head_chars`      | `integer`           | `1500` | `POCKET_AGENT_SPILLOVER_HEAD`      | 注入 Context 的頂部預覽字元長度（不超過門檻的 1/3）。                                            |
| `system.spillover_tail_chars`      | `integer`           | `1500` | `POCKET_AGENT_SPILLOVER_TAIL`      | 注入 Context 的尾部結論字元長度（通常包含測試/建置結論）。                                       |

#### 專案結構鳥瞰圖 (Pocket Repo Map)

| 鍵值 (Key)               | 型別      | 預設值 | 環境變數                       | 說明                                                    |
| :----------------------- | :-------- | :----- | :----------------------------- | :------------------------------------------------------ |
| `system.repo_map_budget` | `integer` | `1024` | `POCKET_AGENT_REPO_MAP_BUDGET` | 注入 Prompt 之 Python AST 簽名結構地圖最大 Token 上限。 |

#### 自動維護管家 (Housekeeping)

> 💡 **全域維護機制**：Housekeeper 會定時（或透過 `/clean` 手動觸發）掃描全域 `~/.pocket-agent/workspaces/` 下所有工作區沙盒，自動清理歷史過期暫存與日誌。

| 鍵值 (Key)                        | 型別      | 預設值 | 環境變數                                | 說明                                                          |
| :-------------------------------- | :-------- | :----- | :-------------------------------------- | :------------------------------------------------------------ |
| `system.housekeeping_enabled`     | `boolean` | `true` | `POCKET_AGENT_HOUSEKEEPING_ENABLED`     | 是否啟用自動日誌與暫存清理機制。                              |
| `system.housekeeping_ttl_days`    | `integer` | `7`    | `POCKET_AGENT_HOUSEKEEPING_TTL_DAYS`    | 暫存落盤與舊備份日誌的保存天數（TTL），過期檔案自動淘汰。     |
| `system.housekeeping_max_files`   | `integer` | `100`  | `POCKET_AGENT_HOUSEKEEPING_MAX_FILES`   | 單一暫存目錄下的最大檔案數量上限，超出時由舊到新刪除。        |
| `system.housekeeping_max_size_mb` | `integer` | `50`   | `POCKET_AGENT_HOUSEKEEPING_MAX_SIZE_MB` | 暫存落盤目錄的容量配額上限（MB），超出時清理至 80% 安全水位。 |

#### 線上自動更新（僅限 Modern Portable 打包版）

> 💡 **環境隔離保護**：線上更新僅於 Modern Portable 打包版（含 `app/PORTABLE` 標記）生效，直接追蹤公開 GitHub 主分支最新 Commit SHA 並自動下載快照套用，無須手動建立 Release；開發人員使用 `run.bat` 執行原始碼時完全停用。

| 鍵值 (Key)             | 型別      | 預設值   | 環境變數                     | 說明                                                                                |
| :--------------------- | :-------- | :------- | :--------------------------- | :---------------------------------------------------------------------------------- |
| `system.auto_update`   | `boolean` | `true`   | `POCKET_AGENT_AUTO_UPDATE`   | 啟動時是否於背景自動檢查並預備最新 Commit 程式碼，並於關閉視窗時自動套用更新。      |
| `system.update_repo`   | `string`  | `""`     | `POCKET_AGENT_UPDATE_REPO`   | 公開 GitHub 儲存庫（格式 `owner/repo`）。留空時自動採用 `BUILD_INFO.json` 記錄值。  |
| `system.update_branch` | `string`  | `"main"` | `POCKET_AGENT_UPDATE_BRANCH` | 線上更新追蹤的 GitHub 分支名稱（預設為 `"main"`）。                                 |

---

### 2.3 提供者設定 (`providers`)

#### 1. OpenAI (`providers.openai`)

- `api_key`: API 金鑰（或由 `OPENAI_API_KEY` 環境變數提供）。
- `base_url`: API 端點位置（預設連線官方端點）。
- `auth_url`, `token_url`, `redirect_uri`: OAuth 登入流程端點。
- `client_id`: OAuth Client ID。
- `image_wrapper_model`: 用於包裝圖片處理的中介模型。

#### 2. OpenRouter (`providers.openrouter`)

- `api_key`: OpenRouter API 金鑰（環境變數: `OPENROUTER_API_KEY`）。
- `base_url`: 預設為 `"https://openrouter.ai/api/v1"`（環境變數: `OPENROUTER_BASE_URL`）。
- `referer`, `title`: 呼叫時夾帶的標頭資訊。

#### 3. Ollama 本地端 (`providers.ollama`)

- `base_url`: 本地端服務網址，預設為 `"http://localhost:11434"`（環境變數: `OLLAMA_BASE_URL`）。

#### 4. Ollama Cloud (`providers.ollama_cloud`)

- `api_key`: 雲端存取金鑰。
- `base_url`: 預設為 `"https://ollama.com/v1"`。

#### 5. SambaNova / Groq / Gemini

- 可直接在 `providers` 下設定 `api_key` 與 `base_url`，或使用對應環境變數（如 `GROQ_API_KEY`, `GEMINI_API_KEY`）。

---

## 3. 常見配置範例

### 範例一：自訂大上下文與落盤參數

適用於大模型（例如 Gemini 1.5 Pro / Claude 3.5 Sonnet / GPT-4o）：

```json
{
  "active_provider": "openai",
  "active_model": "gpt-6.1-sol",
  "system": {
    "context_window": 128000,
    "spillover_threshold_chars": 8000,
    "spillover_head_chars": 2000,
    "spillover_tail_chars": 2000,
    "repo_map_budget": 1500
  }
}
```

### 範例二：自訂 Housekeeping 自動管家

適用於磁碟空間較緊湊的環境：

```json
{
  "system": {
    "housekeeping_enabled": true,
    "housekeeping_ttl_days": 3,
    "housekeeping_max_files": 50,
    "housekeeping_max_size_mb": 20
  }
}
```
