# 功能修復計畫：自動跳過未配置 ESLint 的 JS/TS 專案

### 🧭 任務形狀

- 任務類型：`bugfix`
- 專案類型：`non-app`
- 複雜度：`Lite`
- 風險等級：`Low`

### 📂 相關檔案與既有模式

- [src/auto-lint.js](file:///D:/_Personal/Repositories/Git/ai-agent-demo/src/auto-lint.js)：
  目前在非 Python 且無 Shell 腳本的專案中，會無條件執行 ESLint 流程。若專案未安裝 ESLint 且未設定設定檔，會強制調用全域的 ESLint，導致其因無設定檔而執行失敗，並讓 AI 試圖為其產生設定檔卻又因為環境無對應套件而重新驗證失敗。

### 📖 使用者故事

作為一個自動化 Lint 維護工具，我希望在掃描各個專案時，能夠主動跳過那些「未配置 ESLint」的 JavaScript/TypeScript 專案，避免不必要的執行失敗、自動回退與 Token 浪費。

### ✅ 驗收標準

1. **未配置 ESLint 專案跳過驗收**
   - **Given**：一個含有 `package.json` 但 `package.json` 無 `eslint` 依賴、無包含 `eslint` 的 script，且根目錄無任何 `eslint.config.*` 或 `.eslintrc.*` 設定檔的專案（如 `defi-sentinel-dashboard`）。
   - **When**：執行 `npm run lint:auto` 掃描該專案。
   - **Then**：工具應在日誌中顯示 `🟢 跳過 (未配置 ESLint): 專案名稱`，且狀態回傳為成功，且不應執行任何 ESLint 檢查或 AI 設定檔生成。
2. **已配置 ESLint 專案正常執行驗收**
   - **Given**：一個有配置 ESLint 的專案（如含有 `eslint.config.js` 或 package.json 含有 eslint 依賴）。
   - **When**：執行 `npm run lint:auto` 掃描該專案。
   - **Then**：工具仍會對其執行正常的 ESLint 檢查與修復流程。

### 🚫 不在範圍內

- 修改 `defi-sentinel-dashboard` 專案本身的內容（本次僅修復 `ai-agent-demo` 本身的工具邏輯）。
- 修改 Python 或 Shell 腳本的偵測與修復邏輯。

### 🔒 不可變條件 / Invariants

- 不能影響 Python (ruff/flake8) 與 Shell (shellcheck) 專案的正常掃描。
- 對於本來就有配置 ESLint 的專案，檢查與自動修復流程必須維持原樣。

---

### 🧪 驗證策略 / Verification Strategy

- **Manual verification**：
  在本地執行 `npm run lint:auto -- -d D:\_Personal\Repositories\Git\defi-sentinel-dashboard`。
  確認輸出日誌中 `defi-sentinel-dashboard` 被判定為「🟢 跳過 (未配置 ESLint)」，且不出現驗證失敗回滾的情況。

### 📁 預計異動檔案

| 檔案路徑 | 類型 | 說明 |
| ------------------------------------ | ---- | ---- |
| [src/auto-lint.js](file:///D:/_Personal/Repositories/Git/ai-agent-demo/src/auto-lint.js) | 修改 | 在預設 JavaScript/TypeScript ESLint 流程前，加入 ESLint 痕跡偵測，若無任何 ESLint 痕跡則直接跳過。 |

### ⚠️ 風險點

- 無明顯風險。因偵測機制覆蓋了多種常見 ESLint 配置方式（設定檔、依賴、scripts），誤判率極低。

### 🧯 回復策略 / Rollback Notes

- 若有問題，只需使用 `git checkout -- src/auto-lint.js` 即可還原。

### ❓ 待澄清問題

- 無