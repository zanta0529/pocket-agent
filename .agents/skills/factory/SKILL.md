---
name: factory
description: 軟體工場總協調器。用於功能開發、問題修復、重構、測試、文件、依賴與只讀審查的工作流分流與協調。
---

# Factory Orchestrator

此 skill 是 AI Agent 軟體工場的總協調器。開始前，先探索並遵守目前 repository 中適用的治理入口，例如 `AGENTS.md`、`CLAUDE.md`、`.agents/AGENTS.md`、`.cursorrules`、`.github/copilot-instructions.md`，以及它們路由到的專案規範。依文件宣告的 precedence 套用規則，並保留人類審核點。

## 工具能力

使用目前 Agent 提供的原生工具完成下列能力，不得假設固定工具名稱或參數格式：

- 讀取檔案。
- 搜尋檔名、目錄與檔案內容。
- 對既有檔案進行精準、最小範圍的修改。
- 建立或覆寫已授權的新檔案與 workflow artifacts。
- 執行測試、建置、lint、typecheck、coverage 與 Git 檢查。

## 啟動後的判斷順序

啟動時依序判斷四件事，並在開始前顯示判斷結果讓使用者確認。

### 判斷零：任務類型

| 任務類型      | 條件                                  | 建議流程                                            |
| ------------- | ------------------------------------- | --------------------------------------------------- |
| `feature`     | 新增功能或擴充既有功能                | planner → builder → qa-gate                         |
| `bugfix`      | 修復已知錯誤、測試失敗或行為偏差      | planner → builder → qa-gate，並要求 regression test |
| `refactor`    | 不改變外部行為的內部重構              | planner → builder → qa-gate，強調 invariants        |
| `test-only`   | 只新增、修正或執行測試                | optional skill handoff 或內建測試流程               |
| `docs-only`   | 只新增或修改文件                      | optional skill handoff 或 planner 文件流程          |
| `dependency`  | npm 依賴、漏洞、audit、`package.json` | optional skill handoff 或 npm-only 內建流程         |
| `review-only` | 只做審查、不改檔                      | optional skill handoff 或只讀 planner/review 流程   |

### 判斷一：專案類型

| 類型            | 條件                                                              |
| --------------- | ----------------------------------------------------------------- |
| `backend-only`  | 任務只涉及 API、service、資料庫，無 UI 變動                       |
| `frontend-only` | 任務只涉及元件、頁面、樣式，不需後端變動                          |
| `fullstack`     | 同時需要後端 API 變動與前端 UI 實作                               |
| `non-app`       | 文件、測試、依賴、工具ing、治理或純審查，不直接屬於前後端應用功能 |

### 判斷二：任務複雜度

**Full 模式**（滿足任一）：

- 需新增或修改 API 端點。
- 需資料庫 schema / migration 變動。
- 需求描述模糊，驗收標準不明確。
- 估計影響超過 3 個檔案。
- 專案類型為 `fullstack`。
- 風險等級為 `High` 或 `Critical`。

**Lite 模式**（必須同時滿足）：

- 不新增 API 端點，只修改現有邏輯。
- 不變動資料庫 schema。
- 影響範圍限於 1–2 個檔案。
- 需求描述清晰，預期行為可直接驗證。
- 專案類型為 `backend-only`、`frontend-only` 或明確低風險 `non-app`。
- 風險等級為 `Low` 或 `Medium`。

### 判斷三：風險等級

| 風險       | 條件                                                             |
| ---------- | ---------------------------------------------------------------- |
| `Low`      | 只改文案、測試、簡單純函式、局部 UI                              |
| `Medium`   | 修改服務邏輯、狀態管理、表單驗證、資料轉換                       |
| `High`     | 涉及資料庫、權限、認證、依賴升級、跨模組 API 合約                |
| `Critical` | 可能造成資料遺失、不可逆操作、secrets/auth/multi-tenant 隔離風險 |

風險等級只用於決定流程嚴謹度、停止條件與 QA 深度；不得自動觸發 archive。

## 啟動時輸出格式

```text
🏭 Factory 啟動
任務：[使用者描述]
任務類型：[feature / bugfix / refactor / test-only / docs-only / dependency / review-only]
專案類型：[backend-only / frontend-only / fullstack / non-app]
複雜度：[Full / Lite]
風險等級：[Low / Medium / High / Critical]
建議流程：[planner → builder → qa-gate / optional skill handoff / read-only review]
判斷依據：[1–3 個觸發條件]

繼續？（回覆「繼續」啟動，或直接修正我的判斷）
```

## Artifact Policy

Factory artifact 根目錄由 repository governance 指定；若未指定，預設使用 `.agents/context/`。以下路徑均以預設根目錄示意。無論使用哪個 Agent，都必須讓 Planner、Builder 與 QA Gate 使用同一個有效 artifact 根目錄。

預設維持覆蓋模式：

- `.agents/context/plan.md`
- `.agents/context/api-contract.md`
- `.agents/context/build-summary.md`
- `.agents/context/qa-report.md`

不得因 Full 模式、高風險、QA Fail 或 Conditional Pass 自動建立 archive。

只有當使用者明確要求保留紀錄、建立 archive、另存本次 factory artifact，才建立：

`.agents/context/archive/YYYYMMDD-HHMM-task-slug/`

範例：`.agents/context/archive/20260706-1530-fix-login-validation/`

Archive 建立後也不得自動刪除或清理；只有使用者明確要求清理時才處理。

## Technical Model-Selection Reviews

For a `review-only` comparison of local AI or embedding models, ground the recommendation in both the repository and an executable compatibility check:

1. Trace every model use: document indexing, query embedding, chunking/classification, reranking, and vector-store distance/index configuration.
2. Inspect the persisted-index contract: dimensions, normalization, task prefixes/instructions, and incremental-build state.
3. If it can be done without altering application data, load each candidate through the project's installed runtime and generate an embedding to verify actual compatibility. A small labelled retrieval smoke test is directional evidence only, never a substitute for a representative benchmark.
4. Separate shape compatibility from semantic compatibility. Equal dimensions never justify mixing vectors from different embedding models.
5. State migration requirements precisely: a model change normally requires rebuilding model-derived chunks, clearing or versioning the vector collection, resetting or versioning incremental-build state, and rerunning retrieval evaluation on a representative labelled corpus.
6. Prefer the model whose documented language and task training aligns with the repository's corpus and query language; do not infer suitability solely from a general benchmark or model-card claim.

## Optional Skill Handoff

Factory 可以使用 supporting skills，但它們是 soft dependencies，不是必要條件。

規則：

1. 若目前 Agent 支援 skill activation 且 supporting skill 可用，使用該 Agent 的原生機制啟用並遵循。
2. 若目前 Agent 不支援具名 skill activation，或 supporting skill 不可用，使用 factory 內建 fallback 流程。
3. 不得因 optional skill 不存在而中止任務。
4. 若 optional skill 與專案治理文件衝突，以有效的 repository governance precedence 為準。

| 任務類型                   | Optional skill            | Fallback                                                                                        |
| -------------------------- | ------------------------- | ----------------------------------------------------------------------------------------------- |
| `test-only` / test failure | `vitest-tester`           | 讀 `package.json` 找測試 scripts                                                                |
| `dependency` / audit       | `dependency-guard`        | npm-only 手動流程                                                                               |
| format / lint              | `auto-governance`         | 讀 `package.json` 找 lint/format scripts                                                        |
| `docs-only`                | `generate-system-docs`    | planner 做文件分析與計畫                                                                        |
| `review-only`              | `code-review-assessment`  | planner 做只讀審查                                                                              |
| review remediation         | `code-review-remediation` | Factory 保持唯一協調器；supporting skill 提供 finding 修復策略、regression tests 與 QA 驗收條件 |

### Review remediation ownership

當使用者核准 Code Review findings 並要求修復時：

1. `factory` 是唯一頂層協調器，負責任務分類、Full/Lite 判斷、人類審核點、artifact policy 與 Planner → Builder → QA Gate 流程。
2. 在 Planner、Builder 與 QA Gate 的相關階段載入 `code-review-remediation`，用於 finding clustering、根因修復順序、regression-test obligations 與 finding-specific acceptance checks。
3. `code-review-remediation` 不得自行重新啟動 Factory、建立平行 plan、控制 artifacts，或切換 Planner/Builder/QA 角色。
4. 若 supporting skill 與已核准 plan 或專案治理衝突，以專案治理與核准 plan 為準，並在 mandatory stop condition 停止。

## Full 模式流程

1. Phase 0：解析並準備有效的 Factory artifact 根目錄（governance override，否則 `.agents/context/`）。
2. Phase 1：啟用 `factory-1-planner`，產出計畫後停止，等待「核准計畫」。
3. Phase 2：核准後啟用 `factory-2-builder`。fullstack 時後端通過檢查後再做前端。
4. Phase 3：啟用 `factory-3-qa-gate`，產出 QA report 後停止，等待使用者確認。

## Lite 模式流程

1. 啟用 `factory-1-planner`，附加指示：輕量任務，聚焦 1–2 個受影響檔案，跳過全面掃描；計畫仍需包含專案類型、任務類型、風險等級、不可變條件、驗收標準、預計異動檔案、驗證策略。
2. 等待「核准計畫」。
3. 啟用 `factory-2-builder`。
4. 啟用 `factory-3-qa-gate`，完整執行品質閘道。

## Mandatory Stop Conditions

任何階段遇到以下情況，必須停止並詢問使用者：

- 需要新增、移除或升級依賴。
- 需要修改資料庫 schema、migration 或 ORM 設定。
- 需要修改 authentication / authorization / permission policy。
- 需要不可逆操作、資料刪除、重置、清空或 destructive command。
- 發現核准 plan 與實際架構不符。
- 實作範圍超出已核准 plan。
- 需要 commit、push 或重寫 git history。
- optional skill 建議的流程與專案治理文件衝突。

## Reference

See `references/software-factory-design-notes.md` for the platform-neutral governance, tool-capability, artifact, verification, and stop-condition contracts shared by all factory phases.

## Pitfalls

- 不要繞過人類審核點。
- 不要 commit/push，除非使用者明確要求。
- 不要把 optional skill 當成硬依賴。
- 不要自動建立或清理 archive。
