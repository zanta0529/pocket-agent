# PocketAgent 專案特定開發指南

本文件依目前版本庫的 README、原始碼、依賴、測試及 GitHub Workflow 設定整理。變更技術棧前重新確認現況。以下架構描述為已觀察現況，開發要求則為後續修改應遵循的準則，並非完整安全稽核結論。

## 1. 專案定位與技術棧

- PocketAgent 是本機 Python AI 代理應用，提供 CLI 模式，支援 Windows、macOS、Linux。
- 最低版本為 Python 3.14；Windows 可攜版發布流程（`.github/workflows/release.yml`）亦使用 Python 3.14。
- 入口為 `src/pocket_agent/main.py`，使用 Click；CLI 使用 Rich／prompt_toolkit。
- 引擎透過四大轉接器支援多元模型供應商：`OpenAIResponsesEngine`（ChatGPT OAuth `openai` 與官方 `openai-api`）、`OpenRouterEngine`（`openrouter`）、`OllamaChatEngine`（本機 `ollama` 與雲端 `ollamacloud`）、`StandardChatCompletionsEngine`（`gemini`、`gemini_oauth`、`groq`、`mistral`、`sambanova`）；非同步流程使用 asyncio。
- 認證包含 OpenAI OAuth PKCE 與 Google Gemini OAuth 雙軌，敏感憑證優先透過作業系統原生 `keyring` 儲存，不支援原生憑證庫時以主密碼保護之 JWE 加密庫（`encrypted_provider`）儲存（拒絕明碼降級）；核心依賴包含 OpenAI SDK、httpx、keyring、mcp、curl_cffi、cryptography、truststore、pathspec、Pillow、pywin32（Windows），另 `requirements.in` 尚留有歷史保留依賴 FastAPI／uvicorn。
- MCP 提供外部工具整合：`browser_mcp.py` 為瀏覽器自動化入口（依賴 Playwright，沿用系統 Edge／Chrome），`github_mcp.py` 為 GitHub PR／Issue 檢索與留言入口，`sqlite_mcp.py` 為本機 SQLite 資料庫結構探查與唯讀檢索入口，`mysql_mcp.py` 為遠端 MySQL／MariaDB 資料庫唯讀檢索入口，`mssql_mcp.py` 為遠端 Microsoft SQL Server 資料庫唯讀檢索入口，`memory_mcp.py` 為本機 JSON 跨對話長期記憶檢索入口，`rhel_mcp.py` 為遠端 RHEL 主機結構化唯讀巡檢入口（透過系統 OpenSSH 支援指定私鑰與加固帳密雙軌驗證），`win_rdp_mcp.py` 為遠端 Windows 主機 RDP（Port 3389）視覺控制與結構化唯讀巡檢入口。
- 文件產出（`doc.py`）支援 python-docx、python-pptx、ReportLab；二進位文件讀取（`document.py` 的 `read_document`）支援 `.pdf`、`.docx`、`.pptx`，其中 PDF 讀取優先使用 `pymupdf4llm`（純 `pymupdf`），失敗時降級至 `pypdf`。
- 會話持久化使用 JSON（`~/.pocket-agent/sessions/*.json`，附帶 HMAC-SHA256 數位簽章防竄改），全域系統設定使用 `~/.pocket-agent/config.json`（JSON）與環境變數（`.env`），外部 MCP 伺服器掛載設定使用 YAML（`mcp_config.yaml`）。
- 核心代理之本機索引與快取（RAG FTS5、Skill 檢索、Repo Map AST 快取）一律使用標準庫 `sqlite3`，禁止在核心引入第三方資料庫或 ORM；`pymysql` 與 `pymssql` 僅供 `mcp_servers/` 下之遠端唯讀資料庫 MCP 伺服器使用。

## 2. 目錄與責任邊界

| 路徑                           | 用途與修改原則                                                                                                                                                                                                                |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/pocket_agent/main.py`     | 啟動、模式選擇、內建 MCP 轉發（`--internal-mcp-server`）與 Click 命令（`setup`、`logout`、`update`、`image`）；保留 Windows asyncio 相容處理。                                                                                |
| `src/pocket_agent/core/`       | 核心業務邏輯：Agent 主迴圈（`agent`）、工具派發註冊表（`tool_registry`）、啟動規範載入（`instructions`）、會話紀錄（`sessions`）、活動追蹤（`activity`）、非同步取消與同步橋接（`cancellation`）、附件上傳（`files`）、動態預算（`context_budget`）、歷史摘要壓縮（`compaction`）、巨量輸出落盤（`spillover`）與子代理（`subagents`）。 |
| `src/pocket_agent/engines/`    | LLM 引擎實作：`BaseEngine`（`base`）、`OpenAIResponsesEngine` 與配額策略（`openai_engine`）、`OpenRouterEngine`（`openrouter_engine`）、`OllamaChatEngine`（`ollama_engine`）、`StandardChatCompletionsEngine`（`chat_engine`）與 `EngineFactory`（`factory`）。 |
| `src/pocket_agent/frontend/`   | 使用者介面策略：實作 `cli/`（包含 `main`、`input`、`render`、`state`、`confirmation`、`interrupts`、`commands/`、`completer`、`menus` 等模組）。 |
| `src/pocket_agent/system/`     | 系統基礎設施：全域常數（`constants`）、認證（`auth`）、供應商策略（`providers/`）、路徑解析（`paths`）、設定（`config`）、指令白名單（`permissions`）、Markdown 註冊表（`registry`）、巨集與系統指令（`commands`、`sys_commands`）、設定精靈（`wizard`）、自動清理（`housekeeping`）、相容處理（`compat`）與線上更新（`updater`）。 |
| `src/pocket_agent/extensions/` | 擴充功能：MCP 伺服器生命週期、白名單授權與動態連線參數注入管理（`mcp`）、漸進式技能載入與遮蔽（`skills`）、生命週期事件鉤子（`hooks`）。                                                                                      |
| `src/pocket_agent/tools/`      | 內建工具實作：Shell 執行（`shell`）、原生檔案操作（`fs`）、網路請求（`web`）、圖片生成（`image`）、文件產出（`doc`）、文件讀取（`document`）、FTS5 檢索（`rag`）與 AST 結構圖（`repo_map`）。 |
| `mcp_servers/`                 | 隨附之本地 Stdio MCP 伺服器腳本（`browser_mcp.py`、`github_mcp.py`、`sqlite_mcp.py`、`mysql_mcp.py`、`mssql_mcp.py`、`memory_mcp.py`、`rhel_mcp.py`、`win_rdp_mcp.py`）。                                                     |
| `subagents/`                   | 隨附之內建子代理人設定義檔（`.md`）。                                                                                                                                                                                         |
| `build_scripts/`、`scripts/`   | 可攜版打包腳本（`build_scripts/build_portable.py`）與依賴完整性檢查工具（`scripts/check_imports.py`）。                                                                                                                       |

_其餘如 `.agents/skills/` 為開發代理技能，`tests/` 為回歸測試，`docs/` 為工程規範與架構文件。_

### 2.1 架構設計原則：Adapter／Strategy／Factory／Command

設計目標是讓模型供應商、前端介面與工具執行可替換，避免為每一種組合維護獨立流程。**後續開發必須遵循「依賴抽象、封裝差異、共用流程集中維護」的設計風格。**

- **Adapter（轉接器）**：
  - 引擎層：`OpenAIResponsesEngine`、`OpenRouterEngine`、`OllamaChatEngine`、`StandardChatCompletionsEngine`，將各家 SDK 與串流格式轉換為統一的 `stream_chat` 契約；特定 API 協定輔助方法（如 `format_chat_tools`、`format_chat_messages`）應保留於對應協定基底類別（`StandardChatCompletionsEngine`），不得污染通用 `BaseEngine`。
  - 工具層：核心工具派發統一透過 `core/tool_registry.py`（`ToolHandler` 與 `ToolRegistry`）擴充，禁止在 `Agent._run_loop` 或 `_is_parallel_safe_tool` 散落硬編碼工具分類 `if/elif` 分支。
- **Strategy（策略）**：
  - `CliFrontend` 僅封裝前端的啟動與互動渲染；共同業務流程（包含全域 `instructions.md`／工作區 `AGENTS.md` 載入與連結展開、`<external_content>` 防禦聲明、錯誤格式化與子代理彙整）必須集中於核心層處理。
  - 前端判斷功能支援度（如 `/image`）應查詢 `Agent`／引擎能力旗標（例如 `supports_image_generation`），禁止硬編碼 `provider == "openai"`。
  - 前端指令不得繞過 `Agent` 公開介面直接讀寫 `agent.history`、呼叫私有方法（如 `_run_subagent`）或重複實作連線／限流錯誤提示。
- **Factory（工廠）**：`EngineFactory` 集中選擇及建立引擎；呼叫端應顯式傳入 `provider`，避免依賴 `resolve_provider` 的 URL 推論後備。
- **Command（指令）**：前端斜線指令應透過 `CliCommandRegistry` 註冊各自獨立的 `CliCommand` 實作，禁止以單一巨型 `if/elif` 狀態路由器集中處理多個互動指令。
- **狀態同步**：當指令變更了 Agent 存取權限或全域狀態時，應透過 `Agent._instructions()` 動態注入狀態摘要；業務規則必須統一實作於共用層，前端僅負責接收狀態並渲染。

## 3. 編碼與相容性要求

- Python 使用 4 空白縮排、UTF-8、LF 與檔尾換行；行長嚴格限制為 120 字元以內。
- **Ruff E501 警告防範**：若字串或程式碼超過 120 字元，務必利用 Python 隱式字串拼接（Implicit string concatenation）拆分為多行。若行內包含全形中文字元（CJK），因部分工具與編輯器以雙倍視覺欄寬計算，單行含中文之 docstring、註解與長字串應預留安全餘裕並提早折行。
- **常數集中管理**：凡涉及工具字元上限、子行程緩衝區、逾時秒數與 Context 預算水位之數值常數，必須統一維護於 `src/pocket_agent/system/constants.py`，禁止在個別模組散落硬編碼魔術數字。
- 格式以 `pyproject.toml` 的 Ruff 設定為準：雙引號、E／F／I 規則。
- 新增程式以 Python 3.14 相容為基準。新增／修改介面補型別與必要的錯誤處理。
- 保留取消、失敗提示、附件狀態、連線關閉與 MCP 清理行為，禁止在失敗後偷偷自動重送操作。
- **上下文與草稿區（Context & Scratchpad）**：
  - 專案採用 `ContextBudget` 動態預算管理，計算有效輸入預算（`有效視窗 − 預留輸出 − 安全餘裕`）。
  - 送出前採用統一流程：優先清除舊工具輸出（至目標水位 60%）→ 嘗試 `HistoryCompactor` 歷史摘要壓縮（串流結束後須再次驗證 `cancel_check`，禁止於使用者取消後提交半截摘要）→ 安全滑動視窗降級 → 不可縮減超限強制停止。
  - 使用者可透過 `system.context_window`、`system.context_reserve_output`、`system.context_safety_margin` 與 `system.context_compact_target_ratio` 微調設定。
  - `read_file` 具備 500 行與 12,000 字元雙重限制，並支援 `char_offset` 單行行內字元游標續讀；讀取大型檔案應優先使用 `search_files` 或搭配精準範圍閱讀。
  - 遇到大型 codebase 任務或搜尋結果過長導致截斷時，**必須主動將關鍵發現、變數與 TODO 寫入集中式暫存區**（PocketAgent 執行期沙盒 `~/.pocket-agent/workspaces/<slug>/scratch/notes.md` 或外部開發代理專屬暫存區 `<artifact_dir>`）以建立外部長期記憶；**嚴禁在工作目錄建立 `.agents/scratch`** 避免污染版本庫。詳見 [context-management-architecture.md](context-management-architecture.md) 與 [configuration-specification.md](configuration-specification.md)。

## 4. 資料、擴充與安全邊界

- **資源存取**：隨附資源透過 `bundle_dir()` 取得。PocketAgent 執行期之使用者資料與動態暫存一律集中於 `user_dir()`（`~/.pocket-agent/`，其中日誌與暫存依工作區路徑 Slug 隔離存放於 `~/.pocket-agent/workspaces/<slug>/logs/` 與 `scratch/`）。專案目錄內僅存放受版本控制之靜態規範（如 `AGENTS.md`、`.agents/skills/`），嚴禁在工作目錄隨意建立 `.agents/logs` 或 `.agents/scratch` 污染版本庫。不得將可寫資料塞入唯讀 bundle。
- **既有資料防護**：對話保存在 `sessions/*.json`（含 HMAC-SHA256 簽章），修改格式須考慮向下相容。禁止讀取、列印或提交真實 `.env`、token、私人 session 或 `agent.log`。
- **關鍵安全不變量**：
  - **確認回呼契約（`on_confirm`）**：檔案寫入工具（`fs.py`）與敏感操作僅在 `decision is True` 時放行；若回傳非空字串（使用者自訂指示）或 `(False, str)` 等任何非 `True` 值，一律視為拒絕且不得執行寫入或建立備份。CLI 端透過 `ConfirmationBridge` 統一橋接同步／非同步確認流程，啟用 `Accept-edits` 時須同步套用至主代理與所有 `active_subagents`。
  - **指令白名單邊界（`permissions.py`）**：自訂命令白名單必須完整保留外層 `strip()` 後之參數與引號邊界（大小寫敏感），且 `shlex` 分詞失敗（如未閉合引號）時強制 fail-closed 拒絕。
  - **唯讀資料庫與 RDP 隔離**：資料庫 MCP 強制單一唯讀查詢；`mysql_mcp.py` 採用引號感知之註解正規化（拒絕 `/*!` 可執行註解與引號內反斜線跳脫）與串流游標（`SSCursor`／`buffered=False`，截斷時直接中斷連線不排空剩餘資料）；`win_rdp_mcp.py` 嚴格綁定本工具啟動之系統可信 `mstsc.exe` PID、映像路徑與 `TscShellContainerClass` 視窗類別，截圖強制使用 `ImageGrab.grab(window=hwnd)` 且失焦時立即中止。
  - **引擎工具集邊界**：Chat Completions 串流僅在 `finish_reason == "tool_calls"` 時派發工具呼叫；OpenAI 引擎對 `image_generation` 的參數回退重試僅限移除當輪 `tools` 副本中的 `model` 欄位，嚴禁在 `tools=[]`（如最後一輪禁用工具）時透過回呼重新注入工具。
- **原生工具優先**：進行檔案讀寫或搜尋時，必須優先使用內建原生工具（如 `search_files`、`read_file`）。**嚴禁**使用 shell 指令包裝 `echo`、`sed`、`grep`、`Get-ChildItem` 或 `python -c` 處理檔案讀寫與搜尋操作。
- **環境限制與隔離**：
  - 本專案開發環境預設為 Windows，僅能執行 `cmd.exe`，`PowerShell` 受群組原則封鎖，禁止試圖繞過。
  - 對中文文件明確使用 UTF-8，覆寫檔案前需透過原生工具確認。
- **操作審查**：Agent 不得自動執行測試、Lint、建置、自動安裝依賴、啟動服務或 Git 提交。所有修改狀態或驗證指令必須明確提供給使用者，由使用者手動執行（除非使用者於當次對話明確授權特定指令範圍並經平台審查）。

## 5. 終端機渲染限制（Terminal UI & Rendering）

- **統一色彩語意（`render.py`）**：
  - **動作分類**：`Read`、`Write`、`Action` 等行為字眼一律維持黃色（`bold yellow`）。
  - **工具名稱**：內建工具為綠色（`bold green`）；非內建工具（MCP）為藍青色（`bold cyan`）。
  - **狀態小圓點**：本機唯讀操作為綠色（`bold green`）；寫入或潛在風險操作為紅色（`bold red`）。
  - **安全警告**：「【安全警告】」前綴應單獨使用紅色（`bold red`）。
- **非同步 Spinner 衝突**：在使用 `rich.status`（如「思考中...」動畫）時，**嚴禁**在未停止動畫（`status.stop()`）的狀態下直接呼叫 `input()` 或同步阻塞的標準輸入，以免造成多執行緒渲染衝突導致畫面假死。

## 6. 依賴與發布維護

- `requirements.in`（正式執行期依賴）與 `requirements-dev.in`（開發與測試依賴）為直接依賴來源，`requirements.txt` 與 `requirements-dev.txt` 為 `pip-compile` 產出之鎖定檔；禁止手動修改 `.txt` 鎖定檔。
- 發布 workflow（`.github/workflows/release.yml`）在 Windows／Python 3.14 執行 `build_scripts/build_portable.py`，推送 `v*` tag 會觸發建立 `dist/PocketAgent-portable.zip` Release。修改打包路徑時須同時考慮原始碼、Modern Portable 與 PyInstaller 模式。

## 7. 驗證方式與手動命令

提交與發布前應通過 Ruff 排版與語法檢查、第三方套件依賴完整性掃描（`scripts/check_imports.py`）以及 pytest 自動化測試。新增測試沿用 pytest 既有模式，確保隔離網路、OAuth 與使用者資料；重構核心工具派發路徑時，須留意既有韌性測試（如 `test_agent_resilience.py`、`test_web_resilience.py`）對 `pocket_agent.core.agent.call_*` 之 monkeypatch 相容性，或同步更新測試注入點。

以下命令僅供使用者手動執行（前提為 `venv` 已建立並安裝 `requirements-dev.txt`）：

```cmd
venv\Scripts\python.exe -m ruff format --check .
venv\Scripts\python.exe -m ruff check .
venv\Scripts\python.exe scripts\check_imports.py src scripts
venv\Scripts\python.exe scripts\check_imports.py tests --req requirements-dev.in
venv\Scripts\python.exe -m pytest tests/
venv\Scripts\python.exe -m pytest tests/test_cli_flow.py
```
