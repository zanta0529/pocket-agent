# Aizen 與 PocketAgent 比較與功能參考分析

經過對 `aizen` 與 `pocket-agent` 兩個專案的架構與功能比對，以下為詳細的分析結果與改進建議。

## 結論與整體評估

`aizen` 與本專案 (`pocket-agent`) 的定位非常相似，皆為強大且注重隱私的本地端 AI 代理工具。但兩者的底層技術選型不同：`aizen` 主打 **Rust 靜態編譯、極速啟動 (10ms) 與零依賴**；而 `pocket-agent` 則發揮 **Python 生態系的優勢（尤其是強大的外部轉接、文件生成與快速客製化）**，並且具備獨家的 OAuth PKCE 免費接軌 ChatGPT Plus 資源的架構。

雖然我們無法將 Python 專案重構成 34MB 的靜態執行檔，但 `aizen` 在**代理自治性 (Autonomy)** 與 **系統容錯 (Resilience)** 的設計理念上，有非常多值得 `pocket-agent` 參考並加入的亮點。

以下是建議 `pocket-agent` 可以參考改進的 6 大核心功能：

---

## 1. 驗證提示機制 (Verification Prompts)

- **Aizen 的做法**：在向使用者回報「任務完成」前，會強制通過一道 Verify Gate（例如自動執行 `cargo test` 或型別檢查），如果檢查失敗，AI 會自動讀取錯誤日誌並修復程式碼。
- **本專案可借鑑之處**：
  考量到企業工作站嚴格的 EDR 與群組原則限制，且本專案安全政策禁止代理自動執行測試。因此，當 AI 代理工具完成當次工作、將控制權交還給主流程（使用者）時，不應在背景自動跑測試，而是**主動將相關的測試指令或 CLI 驗證指令條列出來**。使用者可視需求自行在終端機手動執行，以確保安全與系統合規，同時提醒使用者檢驗產出的程式碼。

## 2. 時光機機制 (Native Python Rollback)

- **Aizen 的做法**：利用 Git 建立背景檢查點，提供 `/timemachine` 指令，一旦 AI 改壞了專案，可以瞬間倒轉整個回合的修改。
- **本專案可借鑑之處**：
  針對本機檔案修改的風險，可實作一個 `/undo` 指令。為避免呼叫 `git` 生成子行程而觸發 EDR 警示，應在 `src/pocket_agent/tools/files.py` 發動寫入操作前，純靠 Python 原生的 `shutil` 模組，將修改前的檔案無感備份至 `~/.pocket-agent/backups/`。這能確保時光機機制 100% 在背景靜默運作，完全不受 OS Shell 的限制與稽核阻攔。

## 3. 離線 BM25 長期記憶與技能自我進化 (Offline BM25 Memory & Self-Evolving Skills)

- **Aizen 的做法**：具備離線的 BM25 關鍵字檢索大腦，並能在完成複雜任務後，自動撰寫並保存新技能，日後根據上下文自動召回。
- **本專案可借鑑之處**：
  `pocket-agent` 目前已實作手動觸發的 `/learn` 指令。為解決未來累積大量技能檔所導致的 Context (Token) 溢位問題，強烈建議導入**純本地端的 BM25 向量記憶系統**（可透過 SQLite FTS5 或輕量 Python 套件實作），為專案帶來以下優勢：
  1. **程式碼精準匹配**：BM25 基於詞頻的稀疏向量演算法，對於程式碼變數、特殊命名與錯誤代碼（精確關鍵字）的比對能力，優於神經網路 Embedding。
  2. **零外部依賴與高隱私**：不需要呼叫外部雲端 API 算向量，也不需安裝龐大的 GPU 依賴，完美契合本專案支援 Ollama 斷網開發的理念。
  3. **自動喚醒與上下文最佳化**：使用者提問時，系統在背景用 BM25 快速搜尋 `~/.pocket-agent/skills/`，只將最相關的 Top-K 經驗注入 System Prompt，省下大量 Token 並防止模型分心。
  4. **受控的自動進化**：結合 `config.json` 的授權開關，當 AI 驗證任務成功時便自動將經驗寫入硬碟；搭配 BM25 隨插即用的檢索，達成「越用越聰明」的閉環。

## 4. 災難指令安全底線 (Hard Command Floor)

- **Aizen 的做法**：內建寫死的指令防護，即使使用者開啟了「自動允許 (Auto-Approve)」，也會拒絕執行毀滅性指令（如 `rm -rf /`）。
- **本專案可借鑑之處**：
  `pocket-agent` 雖然已有 WSL/Docker 沙盒與前端的 CLI 確認機制，但建議在後端核心 `src/pocket_agent/tools/shell.py` (或相應命令執行模組) 中，實作一層**靜態正則表達式黑名單**。此底線可防止代理因幻覺而下達刪除系統根目錄等不可逆指令，作為防禦縱深的最後一道防線。

## 5. 多代理平行展開與合成 (Fan-out Sub-agent Synthesis)

- **Aizen 的做法**：提供 `workflow` 概念，將任務平行派發給不同職責的子代理，最後匯總單一結果。
- **本專案可借鑑之處**：
  目前 `pocket-agent` 的 `/subagent <name>` 多為線性委派。未來可在 `src/pocket_agent/core/agent.py` 實作類似 Map-Reduce 的工作流：主 Agent 啟動平行多線程（透過 `EngineFactory` 建立多個實例），分別負責爬梳文件與規劃結構，最後再合成統整，大幅提升複雜重構的處理品質。

## 6. 遠端服務與行動端審批 (Remote Serve & Mobile Approval)

- **Aizen 的做法**：支援 `aizen serve` 背景執行，並將高權限審核推播至手機。
- **本專案可借鑑之處**：
  針對長時運行的腳本任務，`pocket-agent` 可在 `src/pocket_agent/frontend/` 擴充 Headless (無頭) 策略。當觸發風險操作時，透過 Webhook 推播至使用者的通訊軟體，達成遠端非同步協作與審批。

## 7. 互動式設定管理 (Interactive Config Command)

- **本專案衍生新增建議**：
  為配合「自動保存技能」等進階功能的開關，建議於 `src/pocket_agent/system/sys_commands.py` 實作新的 `/config` (或 `/settings`) 系統指令。
  - **檢視與顯示**：使用者輸入 `/config` 時，CLI 以友善格式印出當前 `config.json` 的設定內容。
  - **修改與回寫**：支援帶入參數（如 `/config set auto_learn true`）或透過互動選單 (Prompt Toolkit) 修改設定值，並自動安全地回寫至 `~/.pocket-agent/config.json`。這能讓使用者無需離開代理終端即可即時調整系統行為，大幅提升使用者體驗。

---

## 架構建議

總結來說，`pocket-agent` 最值得優先導入的是 **(1) 驗證閘道** 與 **(2) 時光機 (Git Checkpoints)** 機制。實作時須遵守 `PROJECT_INSTRUCTIONS.md` 的「依賴抽象、封裝差異」原則：將業務規則與驗證邏輯集中於 `core` 與 `tools`，並透過抽象事件與 `frontend/cli` 解耦，即可在不更動核心架構下大幅提升代理的自治性與容錯率。
