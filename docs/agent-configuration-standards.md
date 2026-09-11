# 業界主流 AI 代理工具配置標準 (Agent Configuration Standards)

本文檔整理了主流 AI 代理工具（包含本專案 `Pocket Agent`）在「分層配置 (Layered Configuration)」上的架構慣例與存放位置，作為開發與配置時的參考。

## 為什麼需要分層配置？

業界標準的做法是將配置區分為：

- **全域配置 (Global)**：存放「跨專案通用的工作習慣與技能」（例如：偏好語言、通用自動化腳本），設定一次即可套用於所有專案。
- **專案配置 (Local/Project)**：存放「專屬於該專案的領域知識與架構限制」（例如：該專案使用的特定框架、獨有的部署腳本），用於達成專案間的環境隔離並覆寫全域配置。

---

## 架構核心：疊加與覆寫機制 (Concatenation & Overriding)

業界所有的現代 AI 代理系統（包含 Cursor、GitHub Copilot、Antigravity CLI、Pocket Agent 等），在處理「全域規範」與「專案規範」的衝突時，皆採用**「依序疊加 (Concatenation)」**的策略，而非互相排斥。這項標準是基於底層語言模型的物理特性所制定的：

1. **先載入全域配置**：放置於系統提示詞 (System Prompt) 的最頂端，作為代理的底層常識與基礎人設。
2. **後附加專案配置**：串接於提示詞的最尾端。

### 🧠 Transformer 模型的「近因效應 (Recency Bias)」

現代大語言模型 (LLM) 在處理長文本上下文時，天生存在**近因效應**——模型對於 Prompt 最尾端（最靠近使用者當下提問的位置）的指令，會給予最高的權重與注意力。

業界主流工具正是利用這個特性來實現優雅的覆寫：當底層的全域規範規定「預設使用 Python 開發」，但尾端的專案配置（如 `.cursorrules` 或 `AGENTS.md`）卻規定「本專案嚴格使用 TypeScript」時，模型會自然以最尾端的專案規範為準。這種「由廣到深」的提示詞組裝管線 (Prompt Engineering Pipeline)，實現了極為強大的跨專案配置隔離與繼承。

---

## 代理工具配置對照清單

### 1. Antigravity CLI (agy) & Antigravity IDE

這兩套工具共用 Antigravity 核心引擎，具備極為完整的階層式 (Hierarchical) 掃描機制，是典型的 Markdown 代理架構。

- **【全域】規範檔案**：`~/.gemini/config/` (包含內部定義的 JSON 與全域 rules)。
- **【全域】Skills 檔案**：`~/.gemini/config/skills/`
- **【專案】規範檔案**：專案根目錄的 `GEMINI.md`、`AGENTS.md`，或位於 `.agents/rules/*.md` 內的檔案（會從當前目錄往上尋找至 Git Root）。
- **【專案】Skills 檔案**：專案目錄內的 `.agents/skills/<name>/SKILL.md`（或相容 `.agent/`、`_agents/` 等命名）。

### 2. Claude Code

Claude Code 的設計哲學偏向極簡與設定檔驅動 (Config-driven)，較少依賴分散的 Markdown 檔案。

- **【全域】規範檔案**：`~/.claude.json`
- **【全域】Skills 檔案**：無實體 Markdown 技能檔，依賴 `~/.claude.json` 中掛載的全域 MCP Servers 提供能力。
- **【專案】規範檔案**：專案根目錄下的 `.claude.json`（若存在則覆寫或與全域合併）。
- **【專案】Skills 檔案**：無實體 Markdown 技能檔，通常是透過專案層級的 `.claude.json` 掛載該專案特有的 MCP 伺服器或自訂工具腳本。

### 3. Codex (OpenAI)

_(以 OpenAI 官方原生服務：ChatGPT Custom Instructions / Assistant API 為準)_

- **【全域】規範檔案**：無本機實體檔案。儲存於雲端帳號的 "Custom Instructions" 設定中。
- **【全域】Skills 檔案**：無本機實體檔案。封裝為雲端帳號內的 Custom GPTs 或全域 Functions。
- **【專案】規範檔案**：**無原生支援**。OpenAI 原生介面沒有專案資料夾的概念，必須依賴第三方封裝工具（如 Cursor 的 `.cursorrules` 或是 Github Copilot 的 `.github/copilot-instructions.md`）代為讀取後，作為上下文傳入 API。
- **【專案】Skills 檔案**：**無原生支援**。同上，需由第三方 Client 端實作載入邏輯。

### 4. Hermes Agent

Hermes 是強烈倡導「Thin Router (薄入口)」與「專案內化 Markdown」的代理系統。

- **【全域】規範檔案**：通常依賴於使用者執行的宿主環境 (Host Environment) 或其專屬的系統設定檔。
- **【全域】Skills 檔案**：提供基礎的泛用工具庫（如檔案讀寫、搜尋）。
- **【專案】規範檔案**：嚴格依賴專案根目錄的 `AGENTS.md` 作為路由，並透過其指引動態讀取 `docs/AI_INSTRUCTIONS.md` 及 `docs/PROJECT_INSTRUCTIONS.md`。
- **【專案】Skills 檔案**：通常放置於 `.agents/skills/`，與專案的知識庫緊密結合。

### 5. Pocket Agent (本專案)

架構設計上吸取了 Antigravity 的優點，採用了明確且輕量化的分層與短路求值 (Short-circuit) 設計。

- **【全域】規範檔案**：`~/.pocket-agent/instructions.md`（啟動時自動注入於底層，作為預設系統行為）。
- **【全域】Skills 檔案**：`~/.pocket-agent/skills/<skill-name>/SKILL.md`（需要時動態載入）。
- **【專案】規範檔案**：專案根目錄下的 `AGENTS.md` 或 `.agents/AGENTS.md`（啟動時短路求值尋找單一入口）。
- **【專案】Skills 檔案**：專案根目錄下的 `.agents/skills/<skill-name>/SKILL.md`（優先權高於全域，可覆寫同名通用技能）。
