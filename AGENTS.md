# Hermes / Generic Agent Router Configuration

本檔案是本專案根目錄的「薄索引 / Router」，供 Hermes Agent、Codex 類 coding agent，以及其他會讀取根目錄 `AGENTS.md` 的工具使用。

本專案採用「單一規則庫 (Single Source of Truth)」架構：實質工程規範、開發守則、安全條款與專案特定覆寫，統一維護在 `docs/` 下的指令檔中。本檔案只負責路由與工具層提示，避免把同一套規則複製到不同產品專屬入口。

## 🤖 載入與路由規則 (Routing Protocol)

當代理工具載入本檔案時，必須在開始任何任務、分析或程式修改前，依序檢查並讀取以下規範：

1. **專案特定規範（最高優先）**
   - 檔案：`docs/PROJECT_INSTRUCTIONS.md`
   - 若存在，必須優先讀取並遵守。
   - 若不存在，直接跳過，不要臆造內容。

2. **通用工程治理規範（主體規則庫）**
   - 檔案：`docs/AI_INSTRUCTIONS.md`
   - 必須讀取並遵守其定義的工程開發協定、技術棧偵測、驗證與安全要求。

3. **工具專屬入口（僅補充工具行為，不承載實質工程規範）**
   - Hermes / Generic agents：本檔案。
   - Google Antigravity CLI (`agy`)：`.agents/AGENTS.md`。
   - 若不同工具入口與 `docs/` 規範衝突，除非該工具入口只是為了滿足該工具的安全機制，否則以 `docs/PROJECT_INSTRUCTIONS.md` → `docs/AI_INSTRUCTIONS.md` 的順序為準。

## 🧭 Hermes / Codex 工具層提示

- **強制使用內建工具**：優先使用平台內建檔案工具（如 `read_file`、`search_files`、`patch`、`write_file` / `write_to_file`、`replace_file_content`）進行讀取與修改。建立專案檔案時請勿附帶 Artifact 專用參數（如 `ArtifactMetadata`），以確保正常寫入專案目錄。
- **⚠️ 終端機執行限制 (Group Policy)**：本機環境受群組原則嚴格控管，**群組原則僅作用於終端機命令列**，絕不影響上述內建檔案工具。終端機工具**嚴禁**用於自動執行測試、建置或 lint，亦不得擴大解釋此限制以致放棄內建檔案工具之使用。請嚴格遵守 `docs/AI_INSTRUCTIONS.md` 第 5 節之規定，將驗證指令純文字印出，交由使用者手動執行。
- **不可逆操作防護**：禁止未經使用者要求執行 commit、push、重寫歷史、刪除資料或其他不可逆操作。
- **平台感知**：若必須提供 shell 指令供使用者手動執行，請先判斷當前作業系統（Windows）與環境以提供正確的語法。

## 🧩 跨產品維護原則

- 本檔案與 `.agents/AGENTS.md` 都應保持「薄」。
- 不要在任一工具專屬入口複製完整工程規範；新增或修訂實質規則時，請優先更新：
  1. `docs/PROJECT_INSTRUCTIONS.md`（專案特定覆寫）
  2. `docs/AI_INSTRUCTIONS.md`（通用工程治理）
- 工具專屬入口只記錄該工具必要的載入路徑、工具名稱差異與安全執行方式。
