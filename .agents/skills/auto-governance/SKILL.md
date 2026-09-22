---
name: auto-governance
description: 負責代碼合規治理（Format/Lint 檢查）與自動化 Git Commit 提交
version: 1.0.0
---

# 技能：代碼合規治理與自動提交 (Code Governance & Auto Commit)

本技能定義了 AI Agent 在程式碼編輯完成後，進行合規性檢查、美化排版，以及產生標準 Conventional Commit 訊息並提交的標準作業程序。

## 1. 觸發條件 (Requirements / Triggers)

- 當 AI Agent 完成指定的開發或修正任務，準備將變更提交（Commit）至 Git 版本庫時。
- 使用者要求進行代碼格式化或 Lint 檢查時。

## 2. 工具與相依性 (Tools & Dependencies)

- 本機已配置好 ESLint 與 Prettier。
- 依賴專案內建指令：
  - `npm run format` (使用 Prettier 格式化專案)
  - `npm run lint` / `npm run lint:fix` (執行 ESLint 靜態檢查與自動修復)
  - `npm run lint:verify` (執行 [tools-health-check.js --lint-verify](file:///D:/_Personal/Repositories/Git/ai-agent-demo/src/tools-health-check.js) 驗證合規性)
  - `npm run commit:gen` (執行 [commit.js --suggest](file:///D:/_Personal/Repositories/Git/ai-agent-demo/src/commit.js) 自動產生 Commit Message)

## 3. 執行標準作業程序 (SOP / Steps)

1. **代碼格式化與修復**：
   - 執行 `npm run format` 美化檔案排版。
   - 執行 `npm run lint:fix` 自動修復潛在的 ESLint 語意或排版錯誤。
2. **合規性最終驗證**：
   - 執行 `npm run lint:verify` 以驗證 ESLint 檢查 100% 通過。若有任何錯誤未修復，必須手動修正至無警告無錯誤為止。
3. **Git Stage 暫存**：
   - 使用本機 Git 相關工具將需要提交的檔案進行 Stage (暫存，例如 `git add <file>`)。
4. **生成 Commit 訊息並提交**：
   - 呼叫 `npm run commit:gen` 啟動專案的自動 Commit Message 產生器。
   - 系統將會根據當前的 `git diff` 內容自動推薦符合 Angular 規範的 Conventional Commits 訊息格式（如 `feat(core): ...`、`fix(tester): ...`）。
   - 審查並確認產生的 Commit 訊息，完成 Git Commit。

## 4. 錯誤處理與復原 (Error Handling & Recovery)

- **Lint 驗證未通過**：若 `lint:verify` 回報錯誤且無法被 `lint:fix` 自動修復，必須逐一開啟對應檔案排除問題，嚴禁強行 Commit。
- **Commit 產生器失敗**：若 `npm run commit:gen` 發生錯誤，退回使用常規方式手動撰寫符合 Conventional Commits 規範的訊息。

## 5. 驗證與測試 (Verification)

- 最終確認 `git status` 狀態乾淨（除了已 Commit 的變更）。
- 驗證最新一筆 Git Log 格式是否完全符合規範。
