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

| 鍵值 (Key)        | 型別     | 預設值          | 環境變數                | 說明                                                                                                                     |
| :---------------- | :------- | :-------------- | :---------------------- | :----------------------------------------------------------------------------------------------------------------------- |
| `active_provider` | `string` | `"openai"`      | `POCKET_AGENT_PROVIDER` | 目前啟用的主要模型供應商（如 `openai`, `openrouter`, `azure`, `cerebras`, `ollama`, `ollamacloud`, `sambanova`, `groq`, `mistral`, `gemini`, `openai-compatible`） |
| `active_model`    | `string` | `"gpt-6-astra"` | `OPENAI_MODEL`          | 目前啟用的模型名稱                                                                                                       |
| `locale`          | `string` | `"zh_TW"`       | `POCKET_AGENT_LANG`     | 介面顯示語系（對應 `src/pocket_agent/locales/*.json`，如 `zh_TW`、`en_US`）；可透過 `setup` 或 `/config` 選單切換。     |

---

### 2.2 系統與核心行為設定 (`system`)

```json
{
  "system": {
    "max_tool_rounds": 20,
    "reasoning_effort": "auto",
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

| 鍵值 (Key)                 | 型別      | 預設值             | 環境變數                        | 說明                                                                                                      |
| :------------------------- | :-------- | :----------------- | :------------------------------ | :-------------------------------------------------------------------------------------------------------- |
| `system.max_tool_rounds`   | `integer` | `20`               | `MAX_TOOL_ROUNDS`               | 單輪對話中允許 Agent 連續調用工具的最大輪次上限。                                                         |
| `system.reasoning_effort`  | `string`  | `"auto"`           | `POCKET_AGENT_REASONING_EFFORT` | 預設推理強度（`auto`, `high`, `medium`, `low`, `none`），對話中可用 `/reasoning` (或 `/effort`) 即時切換。 |
| `system.log_level`         | `string`  | `"WARNING"`        | `POCKET_AGENT_LOG_LEVEL`        | 終端機與檔案的日誌記錄等級（可設 `DEBUG`, `INFO`, `WARNING`, `ERROR`）。檔案儲存於全域 `~/.pocket-agent/workspaces/<slug>/logs/agent.log`。 |
| `system.use_sandbox`       | `string`  | `"none"`           | `USE_SANDBOX`                   | 命令列執行沙盒模式（`none` 表示直接本機執行；`docker` 表示透過容器隔離）。                                |
| `system.sandbox_container` | `string`  | `"pocket_sandbox"` | `SANDBOX_CONTAINER`             | 沙盒 Docker 容器名稱。                                                                                    |
| `system.auto_learn`        | `boolean` | `false`            | `POCKET_AGENT_AUTO_LEARN`       | 是否在對話結束後自動分析萃取共用技能。                                                                    |

#### 上下文預算控制 (Context Budget)

| 鍵值 (Key)                            | 型別                | 預設值 | 說明                                                                                       |
| :------------------------------------ | :------------------ | :----- | :----------------------------------------------------------------------------------------- |
| `system.context_window`               | `integer` \| `null` | `null` | 模型 Context 視窗總上限 Tokens。設為 `null` 時由引擎自動向端點探測或套用保底值 (128,000)。 |
| `system.context_reserve_output`       | `integer`           | `4096` | 預留給模型回答與生成內容的空間 Tokens。                                                    |
| `system.context_safety_margin`        | `integer`           | `1024` | 避免臨界抖動的系統防禦緩衝區 Tokens。                                                      |
| `system.context_compact_target_ratio` | `float`             | `0.6`  | 歷史對話壓縮與清理舊工具輸出時的目標水位比例（預留 40% 成長空間）。                        |

#### 大工具輸出落盤虛擬化 (Spill-to-Disk)

| 鍵值 (Key)                         | 型別                | 預設值 | 環境變數                           | 說明                                                                                                                             |
| :--------------------------------- | :------------------ | :----- | :--------------------------------- | :------------------------------------------------------------------------------------------------------------------------------- |
| `system.spillover_threshold_chars` | `integer` \| `null` | `null` | `POCKET_AGENT_SPILLOVER_THRESHOLD` | 啟動工具巨量輸出落盤的字元門檻。設為 `null` 時動態依模型輸入預算之 15% 計算（保底 6,000 字元）。儲存於工作區沙盒 `scratch/outputs/`。 |
| `system.spillover_head_chars`      | `integer`           | `1500` | `POCKET_AGENT_SPILLOVER_HEAD`      | 注入 Context 的頂部預覽字元長度（不超過門檻的 1/3）。                                                                            |
| `system.spillover_tail_chars`      | `integer`           | `1500` | `POCKET_AGENT_SPILLOVER_TAIL`      | 注入 Context 的尾部結論字元長度（通常包含測試/建置結論）。                                                                       |

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

| 鍵值 (Key)             | 型別      | 預設值   | 環境變數                     | 說明                                                                               |
| :--------------------- | :-------- | :------- | :--------------------------- | :--------------------------------------------------------------------------------- |
| `system.auto_update`   | `boolean` | `true`   | `POCKET_AGENT_AUTO_UPDATE`   | 啟動時是否於背景自動檢查並預備最新 Commit 程式碼，並於關閉視窗時自動套用更新。     |
| `system.update_repo`   | `string`  | `""`     | `POCKET_AGENT_UPDATE_REPO`   | 公開 GitHub 儲存庫（格式 `owner/repo`）。留空時自動採用 `BUILD_INFO.json` 記錄值。 |
| `system.update_branch` | `string`  | `"main"` | `POCKET_AGENT_UPDATE_BRANCH` | 線上更新追蹤的 GitHub 分支名稱（預設為 `"main"`）。                                |

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

#### 5. SambaNova / Groq / Mistral / Gemini

- 可直接在 `providers` 下設定 `api_key` 與 `base_url`，或使用對應環境變數（如 `GROQ_API_KEY`, `MISTRAL_API_KEY`, `SAMBANOVA_API_KEY`, `GEMINI_API_KEY`）。

#### 6. Azure OpenAI / Azure AI Foundry (`providers.azure`)

同時支援微軟新一代 `/openai/v1` 統一介面與經典 `api-version` 部署模式（`active_model` 請填寫 Azure 上的 **Deployment Name 部署名稱**）：

- `endpoint`: Azure OpenAI 資源或 AI Foundry 端點位址（環境變數: `AZURE_OPENAI_ENDPOINT`，例如 `"https://<resource>.openai.azure.com"` 或 `"https://<project>.services.ai.azure.com/openai/v1"`）。
- `api_key`: Azure 存取金鑰（環境變數: `AZURE_OPENAI_API_KEY`，必填，儲存於 Keyring 或 JWE 加密庫）。
- `api_version`: 選填 API 版本字串（環境變數: `AZURE_OPENAI_API_VERSION`）：
  - **留空（預設推薦）**：自動採用新一代 `/openai/v1` 介面（`AsyncOpenAI`），若 `endpoint` 未帶 `/openai/v1` 會自動補齊，免維護日期版號。
  - **指定日期版本（如 `"2024-10-21"`）**：若 `endpoint` 未固定指向 `/openai/v1`，將自動切換為經典 `AsyncAzureOpenAI` 部署路由模式。

#### 7. 通用 OpenAI-Compatible 端點 (`providers.openai_compatible`)

適用於 LM Studio、vLLM、llama.cpp 或企業內部 AI Gateway（如 LiteLLM）：

- `base_url`: 完整 OpenAI 相容端點位址（環境變數: `OPENAI_COMPATIBLE_BASE_URL`，例如 `"http://localhost:1234/v1"` 或 `"https://gateway.corp.internal/v1"`）。不會自動補 `/v1`，請依目標服務實際路徑填寫。
- `api_key`: 選填存取金鑰（環境變數: `OPENAI_COMPATIBLE_API_KEY`）；若為免驗證之本機端點可留空。

> 💡 **`ollama` 與 `openai-compatible` 選型差異**：
>
> | 項目 | `ollama`（Ollama Local） | `openai-compatible`（Local / Gateway） |
> | :--- | :--- | :--- |
> | **路徑處理** | 自動補 `/v1`；模型清單走 Ollama 專屬 `/api/tags` | 保留原始 URL（連 Ollama 須自行加 `/v1`）；模型清單走標準 `/models` |
> | **API Key** | 不支援（固定使用本機佔位值） | 選填（支援 Keyring／JWE 保護之 Bearer Token） |
> | **Proxy 行為** | 固定略過系統 Proxy（`trust_env=False`） | 本機迴路（`localhost`／`127.0.0.1`／`::1`）略過 Proxy；其餘位址遵循系統 Proxy（若企業有全域 `HTTP_PROXY`，連內網伺服器請設定 `NO_PROXY`） |
> | **視窗探測** | 自動呼叫 `/api/show` 讀取實際 `context_length` | 不呼叫 `/api/show`，使用保底值（`128,000`）；小視窗模型建議手動設定 `system.context_window` |

#### 8. Cerebras Cloud (`providers.cerebras`)

- `api_key`: Cerebras Cloud API 金鑰（環境變數: `CEREBRAS_API_KEY`，儲存於 Keyring 或 JWE 加密庫）。
- `base_url`: 預設為 `"https://api.cerebras.ai/v1"`（環境變數: `CEREBRAS_BASE_URL`，可於 `setup` 精靈或設定檔自訂）。

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

---

## 4. MCP 伺服器掛載設定 (`mcp_config.yaml`)

外部與內建 MCP 伺服器的掛載與權限設定獨立存放於 `mcp_config.yaml`（範本見 `mcp_config.example.yaml`）。您可在各 `servers.<name>` 區塊中使用 `enabled` 屬性彈性切換程式啟動時是否載入該伺服器：

| 鍵值 (Key)               | 型別      | 預設值 | 說明                                                                                                 |
| :----------------------- | :-------- | :----- | :--------------------------------------------------------------------------------------------------- |
| `servers.<name>.enabled` | `boolean` | `true` | 啟動時是否連線並載入此 MCP 伺服器。設為 `false` 可暫時停用並保留設定；誤填非布林值時會記錄警告並預設停用。 |

```yaml
servers:
  sqlite:
    enabled: true # 可省略，預設即為啟用
    transport: stdio
    command: python
    args: ['mcp_servers/sqlite_mcp.py']

  github:
    enabled: false # 啟動時略過載入，保留 env 與 safe_read_tools 設定
    transport: stdio
    command: python
    args: ['mcp_servers/github_mcp.py']
```

> 💡 更多關於安全注入、白名單與連線參數設定，請參閱：[MCP 伺服器掛載與注入機制](../architecture/mcp-architecture.md)。
