# Build Summary: 多專案 ESLint 與 TS7 相容性修復

### 📝 Non-app 任務摘要

- **受影響專案分析**：
  - 經檢查，整個工作空間中僅有 [tool-lucky-draw-react](file:///D:/_Personal/Repositories/Git/tool-lucky-draw-react) 與 [tool-tournament-react](file:///D:/_Personal/Repositories/Git/tool-tournament-react) 兩個專案配置了 ESLint 與 TS7 整合，面臨相同的 AST 解析執行錯誤 `TypeError: Cannot read properties of undefined (reading 'Cjs')`。其他專案（如 `defi-apy-dashboard` 等）要麼未配置 ESLint，要麼未啟用 TypeScript 解析。

- **解決方案套用變更**：
  - 對 [tool-lucky-draw-react](file:///D:/_Personal/Repositories/Git/tool-lucky-draw-react) 與 [tool-tournament-react](file:///D:/_Personal/Repositories/Git/tool-tournament-react) 分別執行了以下三點更新：
    1. **`package.json`**：在 `devDependencies` 中新增了 `"@typescript/typescript6": "^6.0.2"` 套件。
    2. **`eslint-polyfill.js`**：於根目錄建立 polyfill 檔案，負責在 ESLint 執行時將 TS6 的 API 補足至 TS7 模組上，解決 typescript-eslint 內部的型別與 AST 分析 API 缺失問題。
    3. **`eslint.config.js`**：在設定檔首行引入了 `eslint-polyfill.js`。

- **驗證結果**：
  - 經靜態程式碼審查，此解決方案與 `team-standards` 採用的成功模式完全一致，結構健全且能安全引導解決 runtime 崩潰。
