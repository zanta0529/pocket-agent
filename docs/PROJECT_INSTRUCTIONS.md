# PocketAgent 專案特定開發指南

本文件依目前版本庫的 README、原始碼、依賴、測試及 CI 設定整理。搭配 [AI_INSTRUCTIONS.md](AI_INSTRUCTIONS.md) 使用；變更技術棧前重新確認現況。
以下架構描述為已觀察現況，開發要求則為後續修改應遵循的準則，並非完整安全稽核結論。

## 1. 專案定位與技術棧

- PocketAgent 是本機 Python AI 代理應用，提供 CLI 模式，支援 Windows、macOS、Linux。
- 最低版本為 Python 3.14；一般 CI 與 Windows 發布流程也使用 Python 3.14。
- 入口為 `main.py`，使用 Click；CLI 使用 Rich／prompt_toolkit。
- 引擎包含 OpenAI API 與標準 Chat Completions (如 Gemini, Groq 等)；非同步流程使用 asyncio。
- 認證包含 OAuth PKCE；依賴包含 OpenAI SDK、httpx、FastAPI、uvicorn、keyring。
- MCP 提供外部工具整合；`browser_mcp.py` 為瀏覽器整合入口 (依賴 Playwright)。
- 輸出支援 python-docx、python-pptx、ReportLab；PDF 讀取使用 pypdf。
- session 持久化使用 JSON，設定使用環境變數／YAML；禁止自行引入 SQLite 或 ORM。

## 2. 目錄與責任邊界

| 路徑                           | 用途與修改原則                                                                                       |
| ------------------------------ | ---------------------------------------------------------------------------------------------------- |
| `src/pocket_agent/main.py`     | 啟動、模式選擇與 Click 命令；保留 Windows asyncio 相容處理。                                         |
| `src/pocket_agent/core/`       | 核心業務邏輯：Agent 協調、對話紀錄 (sessions)、上下文與錯誤處理。                                    |
| `src/pocket_agent/engines/`    | LLM 引擎實作：BaseEngine、OpenAI、OpenRouter、Ollama、StandardChatCompletions 與 Factory。           |
| `src/pocket_agent/frontend/`   | 使用者介面策略：實作 `cli/` (包含 input、render、state、commands 等模組)。                           |
| `src/pocket_agent/system/`     | 系統基礎設施：認證 (auth)、路徑解析 (paths)、設定 (config) 與精靈 (wizard)。                         |
| `src/pocket_agent/extensions/` | 擴充功能：MCP (Model Context Protocol) 伺服器生命週期與掛載管理。                                    |
| `src/pocket_agent/tools/`      | 內建工具實作：Shell 執行、原生檔案操作 (fs)、網路請求 (web)、圖片生成 (image)、文件產出 (document)。 |

_其餘如 `.agents/skills/` 為開發代理技能，`tests/` 為回歸測試，`docs/` 為工程規範。_

### 2.1 架構設計原則：Adapter／Strategy／Factory

設計目標是讓模型供應商可替換，避免為每一種組合維護獨立流程。**後續開發必須遵循「依賴抽象、封裝差異、共用流程集中維護」的設計風格。**

- **Adapter（轉接器）**：`OpenAIResponsesEngine`、`OllamaChatEngine`、`StandardChatCompletionsEngine` 等，將各家 SDK 與串流格式轉換為統一的 `stream_chat` 契約。
- **Strategy（策略）**：`CliFrontend` 封裝前端的啟動與互動；共同業務流程交由核心處理。
- **Factory（工廠）**：`EngineFactory` 集中選擇及建立引擎。
- **狀態同步**：當指令變更了 Agent 存取權限或全域狀態時，應透過 `Agent._instructions()` 動態注入狀態摘要；業務規則必須統一實作於共用層，前端僅負責接收狀態並渲染。

## 3. 編碼與相容性要求

- Python 使用 4 空白縮排、UTF-8、LF 與檔尾換行；行長嚴格限制為 120 字元以內。
- **Ruff E501 警告防範**：若字串或程式碼超過 120 字元，務必利用 Python 隱式字串拼接（Implicit string concatenation）拆分為多行。
- 格式以 `pyproject.toml` 的 Ruff 設定為準：雙引號、E／F／I 規則。
- 新增程式以 Python 3.14 相容為基準。新增／修改介面補型別與必要的錯誤處理。
- 保留取消、失敗提示、附件狀態、連線關閉與 MCP 清理行為，禁止在失敗後偷偷自動重送操作。
- **上下文與草稿區 (Context & Scratchpad)**：
  - 專案採用 `ContextBudget` 動態預算管理，計算有效輸入預算（`有效視窗 − 預留輸出 − 安全餘裕`）。
  - 送出前採用統一流程：優先清除舊工具輸出（至目標水位 60%）→ 嘗試 `HistoryCompactor` 歷史摘要壓縮 → 安全滑動視窗降級 → 不可縮減超限強制停止。
  - 使用者可透過 `system.context_window`、`system.context_reserve_output`、`system.context_safety_margin` 與 `system.context_compact_target_ratio` 微調設定。
  - `read_file` 具備 500 行與 12,000 字元雙重限制，並支援 `char_offset` 單行行內字元游標續讀；讀取大型檔案應優先使用 `search_files` 或搭配精準範圍閱讀。
  - 遇到大型 codebase 任務或搜尋結果過長導致截斷時，**必須主動將關鍵發現、變數與 TODO 寫入集中式暫存區**（如 `<artifact_dir>/workspaces/<slug>/scratch/notes.md` 或開發工具專屬暫存區）以建立外部長期記憶；**嚴禁在工作目錄建立 `.agents/scratch`** 避免污染版本庫。詳見 [context-management-architecture.md](context-management-architecture.md) 與 [configuration-specification.md](configuration-specification.md)。

## 4. 資料、擴充與安全邊界

- **資源存取**：隨附資源透過 `bundle_dir()` 取得。使用者資料與執行期動態暫存一律集中於 `<artifact_dir>`（日誌與 scratch 依工作區路徑 Slug 隔離存放於 `<artifact_dir>/workspaces/<slug>/`）。專案目錄內僅存放受版本控制之靜態規範（如 `AGENTS.md`、`.agents/skills/`），嚴禁在工作目錄隨意建立 `.agents/logs` 或 `.agents/scratch` 污染版本庫。不得將可寫資料塞入唯讀 bundle。
- **既有資料防護**：對話保存在 `sessions/*.json`，修改格式須考慮向下相容。禁止讀取、列印或提交真實 `.env`、token、私人 session 或 `agent.log`。
- **原生工具優先**：進行檔案讀寫或搜尋時，必須優先使用內建工具（如 `search_files`、`read_file`）。**嚴禁**使用 shell 指令包裝 `echo`、`sed`、`grep` 或 `python -c` 處理單純的 I/O 操作。
- **環境限制與隔離**：
  - 本專案開發環境預設為 Windows，僅能執行 `cmd.exe`，`PowerShell` 受群組原則封鎖，禁止試圖繞過。
  - 對中文文件明確使用 UTF-8，覆寫檔案前需透過原生工具確認。
- **操作審查**：Agent 不得自動執行測試、Lint、建置、自動安裝依賴、啟動服務或 Git 提交。所有修改狀態指令必須明確提供給使用者，由使用者手動執行。

## 5. 終端機渲染限制 (Terminal UI & Rendering)

- **統一色彩語意 (`render.py`)**：
  - **動作分類**：`Read`、`Write`、`Action` 等行為字眼一律維持黃色 (`bold yellow`)。
  - **工具名稱**：內建工具為綠色 (`bold green`)；非內建工具 (MCP) 為藍青色 (`bold cyan`)。
  - **狀態小圓點**：本機唯讀操作為綠色 (`bold green`)；寫入或潛在風險操作為紅色 (`bold red`)。
  - **安全警告**：「【安全警告】」前綴應單獨使用紅色 (`bold red`)。
- **非同步 Spinner 衝突**：在使用 `rich.status` (如「思考中...」動畫) 時，**嚴禁**在未停止動畫 (`status.stop()`) 的狀態下直接呼叫 `input()` 或同步阻塞的標準輸入，以免造成多執行緒渲染衝突導致畫面假死。

## 6. 依賴與發布維護

- `requirements.in` 為直接依賴來源，`requirements.txt` 為 pip-compile 產出；禁止手改鎖定版本。
- 發布 workflow 在 Windows／Python 3.14 執行 `build_portable.py`，v\* tag 會觸發 release。修改打包路徑時須考慮原始碼、portable 與 PyInstaller 模式。

## 7. 驗證方式與手動命令

一般 CI 執行 Ruff 格式檢查與 pytest。新增測試沿用 pytest 既有模式，確保隔離網路、OAuth 與使用者資料。

以下命令僅供使用者手動執行 (前提為 venv 已建立)：

```cmd
venv\Scripts\python.exe -m ruff format --check .
venv\Scripts\python.exe -m ruff check .
venv\Scripts\python.exe -m pytest tests/
venv\Scripts\python.exe -m pytest tests/test_cli_flow.py
```
