# Antigravity Agent Router Configuration

本檔案為 Google Antigravity CLI (`agy`) 的專屬客製化進入點，作用等同於根目錄 `AGENTS.md` 在 Hermes / generic coding agent 中的「薄索引 / Router」。本專案採用「單一規則庫 (Single Source of Truth)」架構，所有實質工程規範、開發守則與安全防護條款，皆統一維護在 `docs/` 下的指令檔中；本檔案只保留 Antigravity 工具層必要的路由與終端機安全設定。

## 🤖 載入與路由規則 (Routing Protocol)

當 Antigravity 代理人載入本檔案時，必須立即遵循以下路由協定：

1.  **優先加載實質規則庫**：
    在開始任何任務或分析前，請立即讀取並加載位於本專案工作區相對路徑之 [docs/AI_INSTRUCTIONS.md](../docs/AI_INSTRUCTIONS.md) 檔案。
2.  **遵守優先序 (Single Source of Truth)**：
    - **優先順序 1**：`docs/PROJECT_INSTRUCTIONS.md`（專案特定規範，最高優先。**💡 請先確認此檔案是否存在，若不存在則直接跳過此項**）
    - **優先順序 2**：`docs/AI_INSTRUCTIONS.md`（專案工程治理規範，主體規則庫）
    - **優先順序 3**：本設定檔中的路由指示與全域 Skills 底層防護。
3.  **無條件執行**：
    讀取 [docs/AI_INSTRUCTIONS.md](../docs/AI_INSTRUCTIONS.md) 後，無條件遵守其所定義的「台灣 Senior Full-Stack Architect 人設」、「System ACK 協定（最開頭輸出成功載入標籤）」、「AI Agent 工程開發協定 (Universal Engineering Protocol)」。

---

## 🛡️ 終端機執行安全防護協定 (Terminal Safety Protocol)

為嚴格規範 AI 在 CLI 終端機環境中的操作權限，確保系統狀態變更的安全性與透明度，請遵守以下底層防護：

- **嚴禁自動測試與建置 (No Automated Testing)**：本機環境受群組原則控管，**禁止**發起 `run_command` 提案來執行測試、建置或 Linter。請嚴格遵守 `docs/AI_INSTRUCTIONS.md` 第 5 節，將驗證指令純文字印出，由使用者手動執行。
- **強制使用檔案工具與正確參數 (File Tool Rules)**：
  - 進行任何日常檔案任務時，優先使用環境提供的原生檔案工具（如 `read_file`、`write_to_file`、`replace_file_content` 等），嚴禁透過 `run_command` 呼叫 Shell/PowerShell 腳本繞過處理。
  - **建立專案檔案警示**：使用 `write_to_file` 建立專案代碼或設定檔時，**嚴禁**傳入 `ArtifactMetadata`，否則會被限制只能寫入 Artifact 目錄。
  - **邊界釐清**：本機群組原則**僅限制終端機指令（run_command）**，完全不限制原生檔案工具，嚴禁將參數錯誤與群組原則混淆或擴大解釋。
- **極例外狀況的提案審查 (Exception Handling)**：只有在「絕對必要且非腳本類操作」（例如純淨的 Git 狀態查詢或環境變數讀取）時，才允許透過 `run_command` 提案，並於對話中清晰說明原因，嚴禁繞過使用者的 Approve/Reject 審查機制。
- **最小破壞 (Destructive Commands)**：絕對禁止建議或執行不可逆、破壞性或會重置版本紀錄（如強行覆寫 Git 紀錄）的指令。
- **平台感知 (Platform Awareness)**：在提供任何終端機指令給使用者，或發起極例外提案前，必須先判斷當前作業系統（Windows）以提供正確語法。
