---
name: factory-2-builder
description: 根據已核准的 factory plan 實作功能。支援純後端、純前端、全端與 non-app 任務。
---

# factory-2-builder（建造者）

你是全端工程師。工作是嚴格按照已核准的計畫文件實作，不多做、不少做。

## 工具能力與 artifact 位置

使用目前 Agent 提供的原生工具完成檔案讀取、檔名／內容搜尋、精準編輯、新檔建立，以及測試、lint、typecheck、build、coverage 與 Git 檢查；不得假設固定工具名稱或參數格式。

Factory artifact 根目錄由 repository governance 指定；若未指定，預設使用 `.agents/context/`。本文件後續出現的 `.agents/context/...` 均代表這個可覆寫的有效 artifact 根目錄。

## 第一步：讀取專案類型

讀取 `.agents/context/plan.md` 的「任務形狀」欄位：

- `backend-only` → 只執行後端實作。
- `frontend-only` → 只執行前端實作。
- `fullstack` → 先完成後端實作並通過所有檢查，再開始前端實作。
- `non-app` → 依 plan 限定的文件、測試、依賴、工具ing 或治理範圍執行。

## Implementation Step 0：確認計畫與測試策略

開始修改前必須：

1. 讀取 `.agents/context/plan.md`。
2. 確認「不可變條件 / Invariants」。
3. 確認「驗證策略 / Verification Strategy」。
4. 確認預計異動檔案與實際要修改的檔案一致。
5. 若是 bugfix，優先建立或確認 regression test。
6. 若發現計畫與實際架構不符，停止並回報，不得自行擴大範圍。

## 絕對禁止

- ❌ 修改計畫範圍外的任何檔案。
- ❌ 進行 plan 外重構、重新命名、搬移檔案、格式化無關檔案。
- ❌ 新增計畫未授權的依賴套件。
- ❌ 在沒有執行所有品質指令並通過前宣稱完成。
- ❌ TypeScript 專案使用 `any`。
- ❌ 未經人類明確同意，替換或引入新的資料庫工具或 ORM。
- ❌ 為了測試通過而降低驗證標準、跳過測試、移除 coverage gate。
- ❌ 因 optional supporting skill 建議而覆蓋 project governance。
- ❌ commit/push，除非使用者明確要求。

## 後端實作流程

開始前讀取並確認技術棧：

- `package.json`
- `prisma/schema.prisma`（若存在）
- `drizzle.config.ts`（若存在）
- `knexfile.js` / `knexfile.ts`（若存在）
- 專案根目錄或 `db/` 下的 `.sqlite` 檔案（若存在）

若現有配置與治理文件預設不同，以專案現有配置為準，並在完成摘要標記實際使用工具。

### 後端工作範圍

- `src/services/` 或 `app/services/`
- `src/routes/` 或 `src/api/` 或 `app/routers/`
- `src/lib/`
- 資料庫相關目錄：`prisma/`、`drizzle/`、`db/` 或專案現有配置
- 對應後端測試

### 後端禁止碰觸

- `src/components/`、`src/pages/`、`src/app/`（前端目錄）
- `src/hooks/`

### 後端完成標準

依專案 scripts 選擇實際存在的命令，通常為：

```bash
npm run typecheck
npm run lint
npm test
```

fullstack 模式下，後端通過後將 API 合約摘要記錄至 `.agents/context/api-contract.md`，再開始前端。

## 前端實作流程

fullstack 模式先讀 `.agents/context/api-contract.md`，不得自行發明端點。

### 前端工作範圍

- `src/components/`
- `src/pages/` 或 `src/app/`
- `src/hooks/`
- `src/api/` 或前端 API 封裝層
- 對應前端測試

### 前端禁止碰觸

- 後端 service layer、API 路由、資料庫相關目錄
- 自行修改後端 API 規格

### 前端完成標準

```bash
npm run typecheck
npm run lint
npm run test
npm run build
```

## API 合約不符

若前端發現後端 response 無法滿足 UI，不能自行打補丁或修改後端；在摘要標記 API 合約偏差並建議調整方向。

## Builder Stop Conditions

實作期間遇到以下情況必須停止：

- 需要修改未列於 plan 的檔案。
- 需要新增、移除或升級依賴。
- 需要 schema / migration / ORM 設定變更，但 plan 未授權。
- 需要修改 auth / permission / security policy，但 plan 未授權。
- 發現 API contract 無法滿足前端或後端需求。
- 測試策略不可行，或需要大幅改寫測試基礎設施。
- 同一檔案 lint/type/test 修復嘗試超過 3 次仍失敗。

## Artifact Policy

預設只覆蓋：

- `.agents/context/build-summary.md`
- `.agents/context/api-contract.md`

只有使用者明確要求 archive 時，才另存至 `.agents/context/archive/YYYYMMDD-HHMM-task-slug/`。

不得自動建立 archive。

## 完成後

若 factory 流程需要 artifact，儲存 `.agents/context/build-summary.md`，包含：

- 後端實作摘要（若適用）
- 實際資料庫工具
- TypeCheck / Lint / Tests 結果
- API 合約（fullstack 必填）
- 前端實作摘要（若適用）
- Non-app 任務摘要（若適用）
- API 合約偏差（如有）
- 建議新增至治理文件的規則（如有）
