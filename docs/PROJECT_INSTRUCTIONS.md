# PocketAgent 專案特定開發指南

本文件依版本庫現況整理開發與驗證準則，非完整安全稽核結論；變更技術棧前須重新確認。

## 1. 專案定位與技術棧

- PocketAgent 是本機 Python 3.14 AI 代理 CLI 應用（Click ＋ Rich／prompt_toolkit ＋ asyncio），支援 Windows、macOS、Linux。
- 模型供應商透過 `EngineFactory` 與統一轉接介面支援多軌 LLM（詳見 [llm-adapter-architecture.md](architecture/llm-adapter-architecture.md)）。
- 認證採 OpenAI／Google Gemini OAuth PKCE 雙軌，敏感憑證優先存於 OS `keyring`，不支援時改用主密碼保護之 JWE 加密庫（`encrypted_provider`），**嚴禁明碼降級**。
- MCP 工具整合詳見 [mcp-architecture.md](architecture/mcp-architecture.md)；文件產出（`doc.py`）使用 python-docx／python-pptx／ReportLab，二進位讀取（`document.py`）支援 `.pdf`（`pymupdf4llm` 優先、失敗降級 `pypdf`）、`.docx`、`.pptx`。
- 會話以 HMAC-SHA256 簽章 JSON 存於 `~/.pocket-agent/sessions/*.json`，系統設定為 `config.json`／`.env`，外部 MCP 設定為 `mcp_config.yaml`。
- 核心本機索引與快取（統一工作區 FTS5 Trigram 索引、Skill 檢索、Repo Map AST 快取）一律使用標準庫 `sqlite3`，禁止在核心引入第三方資料庫或 ORM（`pymysql`／`pymssql` 限 `mcp_servers/` 資料庫伺服器、`dnspython` 限 `mcp_servers/netdiag/`）。

## 2. 目錄與責任邊界

| 路徑                           | 用途與修改原則                                                                                                                                     |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/pocket_agent/main.py`     | 啟動入口、`--internal-mcp-server` 轉發與 Click 命令；保留 Windows asyncio 相容處理。                                                               |
| `src/pocket_agent/core/`       | 核心業務邏輯：Agent 主迴圈、工具派發註冊表（`tool_registry`）、上下文預算／壓縮／落盤、會話、取消橋接與子代理（`subagents`）。                     |
| `src/pocket_agent/engines/`    | LLM 引擎轉接器與 `EngineFactory`；協定差異保留於對應子類別。                                                                                       |
| `src/pocket_agent/frontend/`   | CLI 介面策略（`cli/` 渲染、輸入、確認、狀態與斜線指令）；僅負責互動與渲染，不放置業務規則。                                                        |
| `src/pocket_agent/system/`     | 基礎設施：常數（`constants`）、多語系（`i18n`／`locales/`）、認證、供應商策略（`providers/`）、路徑、設定、權限、`housekeeping`、更新與系統指令。  |
| `src/pocket_agent/extensions/` | 擴充功能：MCP 生命週期與參數注入（`mcp`）、漸進式技能（`skills`）、鉤子（`hooks`）與外掛（`plugins`）。                                            |
| `src/pocket_agent/tools/`      | 內建工具：`shell`、`fs`、統一工作區 FTS5 索引引擎（`search_index`，供 `fs` 與 `rag` 共用）、`web`、`image`、`doc`、`document`、`rag`、`repo_map`。 |
| `mcp_servers/`                 | 本地 Stdio MCP 薄入口（`<name>_mcp.py`）、領域子套件與共用基礎設施（`_shared/`）；獨立啟動時須確保 `_shared` 在 `sys.path`。                       |
| `subagents/`                   | 隨附唯讀／專職子代理定義檔（`.md`，詳見 [routing-and-subagents.md](architecture/routing-and-subagents.md)）。                                      |
| `build_scripts/`、`scripts/`   | 可攜版打包腳本（`build_portable.py`）與依賴檢查工具（`check_imports.py`）。                                                                        |

_`.agents/skills/` 為開發代理技能，`tests/` 為回歸測試，`docs/` 為規範與架構文件。_

### 2.1 架構設計原則：`minimal-change` 與 Adapter／Strategy／Factory／Command／Template Method

> （**最高優先**）請務必遵循 minimal-change 的精神、本專案既有的 i18n 多語系設計，並且套用 Adapter/Strategy/Factory/Command/Template Method.... 等 Design Patterns 進行規劃設計，降低各模組之耦合度與複雜度。

**後續開發必須遵循「依賴抽象、封裝差異、共用流程集中維護」風格**（新增 LLM Provider 完整檢查清單見 [llm-adapter-architecture.md](architecture/llm-adapter-architecture.md#新增-llm-provider-開發規範檢查清單)）：

- **Adapter（轉接器）**：引擎層統一為 `stream_chat` 契約（API 協定輔助方法保留於對應基底，不污染 `BaseEngine`）；核心工具派發、取消收尾（`on_cancel`）、敏感判定與調研上限集中於 `core/tool_registry.py`，禁止在 `Agent._run_loop` 散落硬編碼工具分類 `if/elif`。
- **Strategy（策略）**：供應商優先繼承 `OpenAICompatibleProvider` 並使用 `PROVIDER_*` 常數；`CliFrontend` 僅處理渲染，共同業務流程集中於核心層，MCP 動態參數註冊於 `CONN_PARAM_SPECS`；前端僅查詢能力旗標（如 `supports_image_generation`），不得硬編碼 provider 或繞過 `Agent` 公開介面（應透過 `Agent.spawn_background_task()` 等）。
- **Factory（工廠）**：`EngineFactory` 顯式依 `provider` 建置引擎，避免依賴 URL 推論後備。
- **Command（指令）**：斜線指令透過 `CliCommandRegistry` 註冊獨立 `CliCommand`，禁止單一巨型路由器。
- **Template Method（樣板方法）**：當多個實作共用同一套固定流程骨架、僅少數步驟或宣告屬性不同時使用——將主流程與防禦邏輯集中於基底，子類別只覆寫必要鉤子或屬性（如 `MarkdownRegistry` 的 `get_glob_pattern`／`parse_item`、`OpenAICompatibleProvider`、`StandardChatCompletionsEngine`），禁止複製貼上共用流程；若無共用骨架、僅需動態替換演算法或策略，優先採 **Strategy**（組合優於繼承）。
- **狀態同步**：權限或狀態異動透過 `Agent._instructions()` 動態注入；業務規則留於共用層。
- **MCP 模組化與命名空間**：複雜 MCP 伺服器採「`_shared/` ＋ `<domain>/` ＋ `<name>_mcp.py`」搭配 `create_mcp_server`；所有 MCP 工具強制冠上 `mcp__{normalized_server}__{tool}`（由 `extensions/mcp.py` 統一正規化與剝除），`allowed_tools` 採嚴格完整名稱比對，`/mcp` 顯示精簡原名並依終端寬度動態排欄。

## 3. 編碼、上下文與多語系規範

- **格式與相容**：Python 3.14、4 空白縮排、UTF-8、LF、檔尾換行，行長嚴格 `<= 120` 字元；含中文字串／註解應預留雙倍寬度餘裕並以隱式字串拼接提早折行（Ruff 雙引號、`E/F/I`）。數值上限／逾時／水位常數統一維護於 `src/pocket_agent/system/constants.py`，新增介面補齊型別與錯誤處理，禁止失敗後偷自動重送。
- **上下文與草稿區**：送出前流程與 `ContextBudget`／`read_file` 雙重上限詳見 [context-management-architecture.md](architecture/context-management-architecture.md) 與 [configuration-specification.md](guides/configuration-specification.md)。關鍵紅線：`HistoryCompactor` 串流結束後須再次驗證 `cancel_check`（嚴禁取消後提交半截摘要）；外部筆記一律寫入集中沙盒 `~/.pocket-agent/scratch/<session_id>/notes.md` 或外部 `<artifact_dir>`，**嚴禁在工作目錄建立 `.agents/scratch`**。

### 3.1 多語系（i18n）開發規範

- **語系檔維護**：所有使用者可見文字一律維護於 `src/pocket_agent/locales/*.json`（`zh_TW.json`、`en_US.json`），透過 `pocket_agent.system.i18n.t(key, **kwargs)` 讀取，**嚴禁寫死顯示文字或冗餘 `default` 文案**；異動時須同步更新所有語系檔並維持佔位符一致（查無鍵值時直接回傳 `key`）。
- **解析順序**：`set_locale()` ＞ `POCKET_AGENT_LANG` ＞ `config.json` `locale` ＞ `zh_TW`（`setup` 精靈未設定時預設 `en_US`）。
- **不可翻譯邊界**：內部工具名稱、送往 LLM 的 System Prompt／Instructions、內部狀態前綴（如 `[CONN_AUTH]`、`[ERROR]`、`ConfirmationBridge._is_fs_edit`）及後台 `logging` 訊息不得納入 UI 多語系替換。
- **獨立模組與測試隔離**：`mcp_servers/` 優先匯入 `mcp_servers._shared.t`；`tests/conftest.py` 透過 `autouse=True` 固定測試語系為 `zh_TW`。

## 4. 資料、安全邊界與環境限制

- **資料與秘密隔離**：隨附資源經 `bundle_dir()` 存取，執行期資料集中於 `user_dir()`（`~/.pocket-agent/`），嚴禁在專案工作目錄建立 `.agents/logs` 或 `.agents/scratch`；`sessions/*.json`（HMAC-SHA256 簽章）須維持向下相容，禁止讀取、列印或提交真實 `.env`、token、私人 session 或 `agent.log`。
- **關鍵安全不變量**：
  - **確認回呼契約（`on_confirm`）**：檔案寫入與敏感操作僅在 `decision is True` 時放行，任何非 `True` 回傳均視為拒絕；`Accept-edits` 須同步套用至 `active_subagents`；`github_create_pr` 強制完整預覽確認且不提供自動 Merge。
  - **指令白名單邊界（`permissions.py`）**：保留外層 `strip()` 後之參數與引號邊界（大小寫敏感），`shlex` 失敗時 fail-closed 拒絕。
  - **唯讀資料庫／RDP／引擎邊界**：資料庫 MCP 強制單一唯讀查詢、引號感知檢查與容量熔斷，RDP MCP 強制綁定可信視窗且失焦中止（細節見 [mcp-architecture.md](architecture/mcp-architecture.md)）；Chat Completions 僅於 `finish_reason == "tool_calls"` 派發工具，且嚴禁於 `tools=[]` 時重新注入工具。
- **環境與操作審查**：開發環境預設為 Windows，**僅能執行 `cmd.exe`**（`PowerShell` 受群組原則封鎖，禁止繞過）；中文文件使用 UTF-8。Agent 不得自動執行測試、Lint、建置、安裝依賴、啟動服務或 Git 提交，驗證指令一律交付使用者手動執行。

## 5. 終端機渲染限制（`render.py`）

- **色彩語意**：`Read`／`Write`／`Action` 為 `bold yellow`；內建工具 `bold green`、MCP 工具 `bold cyan`；唯讀狀態圓點 `bold green`、寫入或風險操作 `bold red`；「【安全警告】」前綴 `bold red`。
- **Spinner 防假死**：呼叫 `input()` 或阻塞標準輸入前，**嚴禁**在未 `status.stop()` 的狀態下進行。

## 6. 依賴與發布維護

- `requirements.in` 與 `requirements-dev.in` 為直接依賴來源，禁止手動修改 `pip-compile` 產出之 `.txt` 鎖定檔。
- `.github/workflows/release.yml` 於 Windows／Python 3.14 執行 `build_scripts/build_portable.py`；修改打包路徑時須同時兼顧原始碼、Modern Portable 與 PyInstaller 三種模式。

## 7. 驗證方式與手動命令

提交與發布前應通過 Ruff、依賴掃描（`scripts/check_imports.py`）與 pytest 測試（隔離網路、OAuth 與使用者資料）：

- **測試配合主程式重構原則（不為測試保留內部死碼）**：重構主程式之內部實作、私有函式／屬性（`_` 前綴）或模組依賴邊界時，**不得為了迎合既有白箱測試（如過時的 `monkeypatch` 目標、私有方法呼叫或舊版別名）而在主程式保留無作用的相容層或死碼**，應於計畫中一併同步更新受影響的測試程式以對齊新架構。
- **不可放寬的邊界**：此原則僅適用於「消除測試對內部實作細節的耦合」；**嚴禁**藉由刪除或弱化行為／安全斷言來強迫測試通過，且第 4 節所列使用者持久化資料與設定格式（`sessions/*.json`、`config.json`、`mcp_config.yaml` 等）仍須維持向下相容。

以下命令僅供使用者於 `cmd.exe` 手動執行（前提為 `venv` 已安裝 `requirements-dev.txt`）：

```cmd
venv\Scripts\python.exe -m ruff format --check .
venv\Scripts\python.exe -m ruff check .
venv\Scripts\python.exe scripts\check_imports.py src scripts --req requirements.in --ignore tiktoken
venv\Scripts\python.exe scripts\check_imports.py tests --req requirements.in --req requirements-dev.in
venv\Scripts\python.exe -m pytest tests/
venv\Scripts\python.exe -m pytest tests/test_cli_flow.py
```
