---
name: vitest-tester
description: 負責執行、生成與修復 Vitest 單元測試，並驗證專案測試覆蓋率
version: 1.0.0
---

# 技能：Vitest 單元測試與自動修復 (Vitest Testing & Fixing)

本技能定義了 AI Agent 在執行專案單元測試、新增測試案例以及因測試失敗進行代碼修復時的標準作業程序。

## 1. 觸發條件 (Requirements / Triggers)

- 當使用者要求執行測試（例如「執行測試」、「跑單元測試」）。
- 當開發新功能完畢，需要為新程式碼撰寫測試案例。
- 當執行測試失敗（如 `vitest` 回傳錯誤碼）需要定位並修復 bugs 時。

## 2. 工具與相依性 (Tools & Dependencies)

- 本機 NPM 環境已安裝 `vitest`。
- 依賴專案內建指令：
  - `npm run test` (啟動監聽模式)
  - `npm run test:run` (執行單次測試)
  - `npm run test:coverage` (產生測試覆蓋率報告)
  - `npm run test:gen` (調用 [generate-tests.js](file:///D:/_Personal/Repositories/Git/ai-agent-demo/src/generate-tests.js) 自動產生測試骨架)

## 3. 執行標準作業程序 (SOP / Steps)

1. **執行既有測試**：優先使用 `run_command` 執行 `npm run test:run`，確認當前測試狀態。
2. **新增測試骨架（若需要）**：
   - 當需要針對新模組編寫測試，且測試檔案不存在時，執行 `npm run test:gen` 自動為目標檔案生成相對應的單元測試骨架。
   - 根據產生的骨架，手動補齊測試案例與邊界條件。
3. **錯誤定位與修復 (Debug Loop)**：
   - 閱讀測試失敗的詳細 Stacktrace 與錯誤訊息。
   - 找出測試不通過的原因（主程式逻辑錯誤或測試斷言不合理），進行對應的代碼修改。
   - 每次修改後，再次執行 `npm run test:run` 直到所有測試完全通過（綠燈）。
4. **測試覆蓋率驗證**：
   - 通過測試後，執行 `npm run test:coverage` 驗證測試覆蓋率是否達到專案標準。

## 4. 錯誤處理與復原 (Error Handling & Recovery)

- **依賴遺漏**：如果執行時提示缺少 `@vitest/ui` 等依賴，先執行 `npm install` 後再嘗試。
- **無限循環防護**：如修改後測試依然失敗且累計次數超過 5 次，應立即停止修改，向使用者報告具體衝突點，要求人工介入審查。

## 5. 驗證與測試 (Verification)

- 最終確認 `npm run test:run` 執行結果為 `PASS`。
- 覆蓋率報告中未出現顯著下降。
