---
name: dependency-guard
description: 負責 NPM 依賴完整性檢查、安全漏洞審查與防禦性包升級
version: 1.0.0
---

# 技能：依賴防護與安全升級 (Dependency Guard & Security Update)

本技能定義了 AI Agent 在安裝新套件、管理 `package.json` 中的依賴、以及應對安全漏洞審查時的標準作業程序。

## 1. 觸發條件 (Requirements / Triggers)

- 當使用者要求「安裝套件」、「升級依賴」或「更新 package.json」時。
- 當需要檢查專案是否有冗餘或缺失的 NPM 依賴時。
- 當需要進行專案安全審查與修復漏洞（如 `npm audit` 發現威脅）時。

## 2. 工具與相依性 (Tools & Dependencies)

- 本機 NPM 與 Node.js 環境。
- 依賴專案內建指令與工具：
  - `npm run deps` (呼叫 `depcheck` 檢查專案依賴狀態)
  - `npm run audit` (呼叫 `npm audit` 掃描安全性漏洞)
  - `npm run test:run` (執行單元測試進行相容性驗證)

## 3. 執行標準作業程序 (SOP / Steps)

1. **依賴完整性檢查**：
   - 執行 `npm run deps` 檢查是否有「已安裝但未使用」的冗餘套件（Unused dependencies），或「已在代碼中使用但未安裝」的缺失套件（Missing dependencies）。
   - 針對檢查結果，移除冗餘依賴或補上缺失依賴。
2. **漏洞掃描**：
   - 執行 `npm run audit` 檢查當前依賴樹中是否存在 High 或 Critical 等級的已知漏洞。
3. **防禦性升級 (Safe Upgrade)**：
   - 升級時，應優先採用語義化版本（SemVer）的安全升級原則，以**小版本升級 (Patch/Minor)** 為主，避免強行升級主版本 (Major) 造成破壞性變更。
   - 執行 `npm install` 進行更新。
4. **相容性回歸測試**：
   - 依賴變更後，**強制**執行 `npm run test:run`。必須確認所有單元測試皆為綠燈，方可認定升級安全。

## 4. 錯誤處理與復原 (Error Handling & Recovery)

- **升級導致測試失敗**：若升級依賴後單元測試報錯，代表存在破壞性不相容變更。應立即執行 `git checkout package.json package-lock.json` 退回原狀態，改採手動分析相容性。
- **無法修補之漏洞**：若漏洞因底層依賴關聯無法被 `npm audit fix` 解決，應向使用者提出架構報告，尋求替代方案。

## 5. 驗證與測試 (Verification)

- `npm run deps` 的檢查報告中沒有 Missing 項目。
- `npm run audit` 報告中無 High / Critical 漏洞威脅。
- 升級後專案編譯正常，且 `npm run test:run` 全部通過。
