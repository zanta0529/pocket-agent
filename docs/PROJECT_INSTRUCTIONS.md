# PocketAgent 專案特定開發指南

本文件依目前版本庫的 README、原始碼、依賴、測試及 CI 設定整理。
搭配 [AI_INSTRUCTIONS.md](AI_INSTRUCTIONS.md) 使用；變更技術棧前重新確認現況。
以下架構描述為已觀察現況，開發要求則為後續修改應遵循的準則，並非完整安全稽核結論。

## 1. 專案定位與技術棧

- PocketAgent 是本機 Python AI 代理應用，提供預設 GUI 與 CLI，README 宣告支援 Windows、macOS、Linux。
- README 最低版本為 Python 3.11；一般 CI 與 Windows 發布流程也使用 Python 3.11。
- 入口為 `main.py`，使用 Click；GUI 使用 CustomTkinter／Tkinter，CLI 使用 Rich／prompt_toolkit。
- 引擎包含 OpenAI Responses API 與 Ollama Chat Completions；非同步流程使用 asyncio。
- 認證包含 OAuth PKCE；相關依賴包含 OpenAI SDK、httpx、FastAPI、uvicorn、keyring。
- MCP 提供外部工具整合；`browser_mcp.py` 為瀏覽器整合入口，依賴中包含 Playwright。
- 文件輸出相關依賴包含 python-docx、python-pptx、ReportLab；PDF 讀取使用 pypdf。
- 已檢閱的 session 持久化使用 JSON，設定使用環境變數／YAML 等；不要自行引入 SQLite 或 ORM。

## 2. 目錄與責任邊界

| 路徑                                                       | 用途與修改原則                                                                                           |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `main.py`                                                  | 啟動、模式選擇與 Click 命令；保留 Windows asyncio 相容處理。                                             |
| `agent/core.py`                                            | Agent 核心協調；修改前追蹤引擎、工具與前端的呼叫關係。                                                   |
| `agent/engines/`                                           | BaseEngine、事件資料型別、factory 與 provider 實作。                                                     |
| `agent/frontend/`                                          | GUI／CLI 策略；CLI 拆分為 input、render、state、commands 等模組。                                        |
| `gui/`                                                     | 圖形介面、元件與 assets；不得將 GUI 依賴無條件引入無頭 CLI 路徑。                                        |
| `agent/auth.py`                                            | 認證與 provider 設定；避免暴露憑證或觸發真實登入。                                                       |
| `agent/mcp_manager.py`                                     | MCP 連線與生命週期。                                                                                     |
| `agent/shell.py`                                           | 命令執行、確認、逾時與編碼處理；屬安全敏感區域。                                                         |
| `agent/fs_tools.py`、`agent/web_tools.py`                  | 原生檔案與網路工具實作；包含嚴密的安全沙箱、網頁解析與人類授權機制。                                     |
| `agent/paths.py`                                           | 開發／PyInstaller／portable 路徑與使用者資料位置。                                                       |
| `agent/sessions.py`                                        | JSON 對話持久化。                                                                                        |
| `agent/registry.py`、`agent/skills.py`、`agent/plugins.py` | 擴充註冊、技能載入與匯入。                                                                               |
| `skills/`、`agents/`、`commands/`                          | 產品隨附的技能、子代理與命令，不等同開發代理規範。                                                       |
| `.agents/skills/`                                          | 開發代理技能；維持通用性，專案差異放在本指南。                                                           |
| `tests/`                                                   | pytest 回歸測試。                                                                                        |
| `build_portable.py`、`PocketAgent.spec`                    | portable 與 PyInstaller 包裝設定。                                                                       |
| `docs/`                                                    | 工程規範與文件；代理入口見根目錄 `AGENTS.md`、`.agents/AGENTS.md` 與 `.github/copilot-instructions.md`。 |

### 2.1 架構設計原則：Adapter／Strategy／Factory

本專案同時支援 Ollama／Cloud AI 與 CLI／GUI，這是兩個應獨立變動的維度。設計目標是讓模型供應商與操作介面可替換，而不是為每一種組合各自維護一套 Agent 流程。

**後續開發必須遵循「依賴抽象、封裝差異、共用流程集中維護」的設計風格。**

#### 設計模式與責任邊界

| 模式／邊界        | 現有對應                                                  | 修改原則                                                                             |
| ----------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Adapter（轉接器） | `BaseEngine`、`OpenAIResponsesEngine`、`OllamaChatEngine` | 將供應商的請求格式、SDK 呼叫與串流回應轉換為統一的 `stream_chat` 與 `Event*` 契約。  |
| Strategy（策略）  | `FrontendStrategy`、`CliFrontend`、`GuiFrontend`          | 封裝不同前端的啟動與互動方式；介面差異留在前端，共同業務流程交由核心處理。           |
| Factory（工廠）   | `EngineFactory.create(provider, client, model)`           | 集中選擇及建立引擎，避免各呼叫端自行判斷供應商並建構具體實作。                       |
| 共用流程協調      | `agent/core.py` 與既有共用模組                            | 集中維護對話協調、串流事件消費、工具回合及其他共用規則，避免依前端或供應商複製流程。 |

以上描述為現有抽象的設計角色與後續修改準則，不代表所有歷史程式碼均已完全解耦。修改前仍須確認實際呼叫路徑與契約，不得只依類別名稱推定行為。

#### 依賴方向與統一契約

- 前端透過共用核心／服務使用能力；核心透過 `BaseEngine` 契約呼叫模型引擎。具體實作的選擇集中在既有入口與 factory。
- 引擎不得依賴 GUI 元件、Rich 或終端機輸入；共用核心不得為呈現需求直接操作視窗或終端機。
- GUI 專用依賴維持延遲載入，不得因新增共用模組而讓無頭 CLI 路徑依賴 Tkinter。
- 供應商的 SDK 回應、訊息與工具參數格式、串流協定差異，應封裝於引擎或其專用轉換模組；不得交由 CLI／GUI 各自解析。
- `BaseEngine.stream_chat` 的參數、非同步串流及 `Event*` 語意屬於共用契約。修改時必須同步檢查各引擎、核心及受影響的前端消費路徑。
- 取消、錯誤傳遞、工具呼叫識別碼、參數分片、完成事件與資源清理也是契約的一部分，不得因替換實作而遺失。
- 不同模型不必支援完全相同的能力；不支援時須明確回報或採用已定義的降級行為，不得偽造成功、默默忽略請求，或偷偷重試可能產生副作用的操作。

#### 避免重複與過度耦合

- 修改前先搜尋既有核心、helper、service 與擴充點。相同業務規則應有單一維護來源，不得將另一個引擎或前端整段複製後分別修改。
- 隨供應商變動的行為留在 Adapter；隨操作介面變動的輸入、呈現與排程留在前端 Strategy；不隨兩者變動的規則留在共用層。
- 供應商／模式判斷並非全面禁止，但應集中於選擇、組裝或真正的差異邊界，不得在核心、工具與各前端反覆增加相同的 `if provider ...`／`if gui ...`。
- 若存在跨層能力差異，先確認既有擴充點，再採最小必要的能力描述或策略；不要以散落的具體類別判斷取代抽象契約。
- GUI 與 CLI 不必逐行共用呈現程式碼，也不得以共用為由將 UI 依賴拉入核心。
- Design Patterns 是管理變動與重複的手段，不是目的。優先沿用既有抽象，避免不必要的繼承階層、工廠或同時包辦認證、SDK、UI 與工具執行的巨型管理類別。

#### AI 代理的擴充與檢閱要求

1. **先辨識修改位置**：追蹤入口、前端、核心、factory、引擎與事件消費路徑，說明需求屬於共用流程、供應商差異或介面差異。
2. **新增供應商**：實作符合 `BaseEngine` 的 Adapter，接入既有 factory 及必要的設定／認證邊界；不得另建 CLI／GUI 專用版本或複製 Agent 迴圈。
3. **新增前端**：沿用 `FrontendStrategy` 與共用核心，再接入入口選擇；不得複製模型呼叫、工具執行、session 持久化或安全確認規則。
4. **新增跨模式功能**：先在共用層實作共同規則，再分別接上 CLI／GUI 的互動呈現；部分組合不支援時，明列限制及處理方式。
5. **檢查契約相容性**：確認抽象與具體實作的參數、回傳值、事件及生命週期一致。既有差異須明確記錄，不假定所有實作已完全可替換，也不藉機擴大無關重構。
6. **規劃差異邊界測試**：使用 fake／mock 隔離外部依賴，檢查 Ollama／Cloud AI × CLI／GUI 四種組合的影響，涵蓋取消、失敗、工具呼叫與清理。不必重複測試相同內部細節，但須涵蓋各差異邊界及必要整合路徑。
7. **交付設計依據**：列出重用的抽象、差異封裝位置、重複流程的處理方式，以及契約與相容性影響。需要偏離上述原則時，先說明理由、影響與替代方案。

測試執行仍須遵守本指南第 5、7 節，不因架構調整而自動執行測試、Lint 或建置。

## 3. 編碼與相容性要求

- Python 使用 4 空白縮排、UTF-8、LF 與檔尾換行；行長以 120 為準。
- Python 格式以 `pyproject.toml` 的 Ruff 設定為準：雙引號、E／F／I 規則；
  `.editorconfig` 的 single quote 提示不覆寫 Python formatter。
- 新增程式以 Python 3.11 相容為基準，不使用僅在更高版本提供的語法或標準庫 API。
- 新增／修改介面補型別與必要的錯誤處理，不為型別改善擴大重構既有程式。
- 保留 `BaseEngine.stream_chat` 的 Event 型別契約；修改串流事件時同步檢查兩個 provider 與所有消費端。
- 避免在 GUI 執行緒阻塞網路與長時間操作；更新元件沿用既有排程模式。
- 保留取消、失敗提示、附件狀態、連線關閉與 MCP 清理行為，不在失敗後偷偷自動重送可能有副作用的操作。
- 使用 pathlib 與集中式路徑 helper，不硬編碼本機磁碟或假定目前目錄就是專案根目錄。
- 文件與使用者訊息的語言遵循通用協定第 6 節；程式識別字沿用現有英文慣例。

## 4. 資料、擴充與安全

- 隨附資源透過 `bundle_dir()` 取得；PyInstaller 使用其 bundle 資源位置。
- `user_dir()` 為 `~/.pocket-agent`；發布版 `.env` 與 MCP 設定走使用者資料目錄，
  開發模式則由 `env_path()`、`mcp_config_path()` 指向版本庫位置。不得將可寫資料塞入唯讀 bundle。
- 對話保存在 `~/.pocket-agent/sessions/*.json`；修改格式須考慮既有資料讀取與遷移。
- 產品技能從 bundle 的 `skills/` 與使用者 `~/.pocket-agent/skills/` 載入，同名時使用者優先；
  SKILL.md 使用 name／description frontmatter。不要將 `.agents/skills/` 當成產品載入目錄。
- 技能／外掛 ZIP、附件、MCP 回應與模型工具參數皆是不可信輸入；維持並補強路徑穿越、覆寫與解壓防護。
- 不讀取、列印或提交真實 `.env`、token、私人 session、瀏覽器狀態或 `agent.log`；需要設定時先讀範例檔。
- 不自動啟動 OAuth、真實模型呼叫、MCP／瀏覽器服務、更新器或外部腳本。
- 修改 shell、hooks、plugins、MCP 或更新流程時，不移除確認機制、逾時或沙箱檢查。
- 應用程式的 shell／sandbox 能力不等於開發代理已獲准執行命令，也不能保證任意命令均被安全隔離。
- 不手動修改或提交 venv、build、dist、快取、產生的圖片／文件與私人設定。

## 5. 專案工具政策與已知環境

### 5.1 專案政策

- **原生工具優先**：Pocket Agent 已內建 `agent/fs_tools.py` 與 `agent/web_tools.py`。進行檔案讀寫、全域搜尋 (grep/find)、或網頁搜尋時，**必須優先使用系統提供的原生工具**。嚴禁使用 `run_command` 包裝 `echo`、`sed`、`grep` 或 `python -c` 腳本來處理單純的 I/O 操作，以避免觸發 EDR 警報與跳脫字元錯誤。
- 對中文文件明確使用 UTF-8，不以有損解碼結果覆蓋原檔。
- 既有檔案覆寫前需透過原生工具進行確認；缺少工具或權限時停止並交付手動方案。
- 開發代理不得自動執行測試、Lint、格式化、建置或 pre-commit；只提供手動命令。
- 不自動安裝依賴、啟動服務、commit、push、打 tag、發布或刪除資料。
- 上述開發代理政策不因 OS 不同而自動放寬；遠端既有 CI 仍依 workflow 執行，不為配合開發代理政策停用 CI。

### 5.2 已知 Windows 工作站限制

- 已知工作站的 Windows 平台工具以 cmd.exe 執行，PowerShell 受群組原則封鎖；不得繞過。此為特定環境紀錄，不表示所有開發環境皆為 Windows。
- 每次切換環境，依當前平台資訊確認 OS、Shell 與工具權限；不把 PowerShell 限制推論為所有 Shell 均不可用，亦不宣稱原生檔案工具永遠不受權限或沙箱限制。
- 在此 Windows 環境缺少原生工具時，經使用者／工具審查授權後，可用 cmd.exe 搭配 Python 做限定範圍的檔案讀寫與 Git 唯讀查詢。其他環境須另確認可用且獲准的替代方式。
- Python 終端機讀寫可使用 `python -X utf8`；此替代機制不是測試、安裝、建置或繞過 OS 政策的許可。

## 6. 依賴與發布維護

- `requirements.in` 為直接依賴來源，`requirements.txt` 是 pip-compile 產出的鎖定結果；不要手改鎖定版本。
- 依賴異動先說明必要性、相容性與影響，再由使用者於授權環境重新產生並檢閱鎖定檔。
- 不直接重播鎖定檔註記的 `--no-index` 指令；該選項是否可用取決於本機套件來源。
- 發布 workflow 在 Windows／Python 3.11 執行 `build_portable.py`，v\* tag 會觸發 release。
- 修改打包或資源路徑時須考慮原始碼、portable 與 PyInstaller 模式；不直接修改 dist 成品。

## 7. 驗證方式與手動命令

一般 CI 在 Ubuntu／Python 3.11 執行 Ruff 格式檢查、Lint 與 pytest。
既有測試涵蓋 auth／Ollama、CLI commands／flow／input／render／state、MCP manager 與 shell sandbox；
這不代表 GUI、打包或所有 provider 均已有完整覆蓋。

新增測試沿用 pytest、unittest.mock／AsyncMock、tmp_path 等既有模式；隔離網路、OAuth、
使用者 home、session、MCP 與子行程，不使用真實憑證或改動真實使用者資料。

以下命令僅供使用者在版本庫根目錄的已授權 Windows cmd.exe 手動執行。
前提為 venv 已建立，且其中已安裝專案依賴及 Ruff／pytest；不存在時不要宣稱可以直接執行。

```cmd
venv\Scripts\python.exe -m ruff format --check .
venv\Scripts\python.exe -m ruff check .
venv\Scripts\python.exe -m pytest tests/
```

針對 CLI 交易／清理行為可先執行：

```cmd
venv\Scripts\python.exe -m pytest tests/test_cli_flow.py
```

回報時分開列出：文件／差異檢閱、實際執行的命令與結果、未執行項目及原因。
本機僅提供命令不等於測試通過；GUI 與打包相關變更仍需使用者額外人工驗證。

## 8. 已知設定差異與後續協調

- README 與 CI 為 Python 3.11，但 Ruff target-version 目前是 py310；
  requirements.txt 檔頭則記錄由 Python 3.14 產生。鎖定依賴能否完整支援 3.11 尚須驗證。
  不把生成版本誤寫成最低執行版本；統一設定或重建鎖定檔需另立變更。
- `.editorconfig` 的 single quote 與 Ruff 的 double quote 不一致；Python 暫以 Ruff 為準。
- 根目錄 `AGENTS.md`、`.agents/AGENTS.md` 與 `.github/copilot-instructions.md` 為薄入口，先引導讀取 `docs/AI_INSTRUCTIONS.md`，
  再讀取本專案指南；不重複維護工程與工具政策。入口存在不保證各代理自動載入，仍須確認工具設定。
- 上述設定差異以 README、`pyproject.toml`、`requirements.txt`、`.editorconfig` 與 `.github/workflows/` 為追查來源；修改相關來源時同步重新確認本節，差異解決後移除過時警告。
- 各平台入口的自動載入狀態尚待實測。驗證時記錄平台／客戶端版本、功能與設定、入口路徑、作用範圍及日期；在新對話中使用無副作用任務，依可觀察的載入紀錄或工具讀取結果判定，不僅依代理口頭宣稱。無法取得證據時標示未確認。
