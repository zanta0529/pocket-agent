# 常見 AI Agent 分層配置與 Context Discovery Patterns (Architecture Spec v1.0)

本規格書（Architecture Spec）旨在定義與比較現代 AI 代理工具在「分層配置 (Layered Configuration)」、「上下文探索 (Context Discovery)」以及「技能管理 (Skills)」上的核心架構設計，並作為 Pocket Agent 實作的正式架構依據。

---

## 1. Scope

現代 AI 代理系統面臨著跨專案開發、多重環境設定與團隊協作等複雜場景。為了維持系統的擴充性與環境隔離性，我們必須明確區分四種截然不同的生命週期物件：**Secrets**、**Runtime Config**、**Instructions (Context)** 與 **Skills**。

本文件將定義這四者的語意 (Semantics)，分析常見代理工具的實作模式，並確立 Pocket Agent 的最終設計架構。

---

## 2. Configuration Model 的四種核心語意

在代理架構中，絕不能將所有設定混為一談。我們將配置模型分為四層不同的處理語意：

### 2.1 Secrets / Credentials（機密憑證）

- **特性**：API 金鑰、OAuth Tokens 等高敏感度資料。
- **處理語意**：**Strict Isolation (嚴格隔離)**。
- **規則**：絕對不可與 Runtime Config 混用，通常由專屬的 Keyring 系統或受保護的環境變數獨立管理。

### 2.2 Runtime Configuration（機制配置）

- **特性**：決定 Agent 系統如何運作的明確參數（例如模型選擇、MCP 伺服器掛載、權限設定）。
- **處理語意**：**Deterministic Merge (決定性合併)**。
- **規則**：透過程式碼邏輯進行深層或淺層合併，當發生衝突時，通常由更具體的層級（Local/Project）直接覆寫較廣泛的層級（Global）。這完全由程式碼控制，與 AI 模型的理解無關。

### 2.3 Instructions / Context（自然語言上下文）

- **特性**：提供給大語言模型 (LLM) 閱讀的自然語言指引（例如架構限制、開發守則、人設）。
- **處理語意**：**Concatenate Chain (依序疊加)**。
- **規則**：不同層級的 Instructions（Global → Project → Subdirectory）通常不會互相排除，而是透過 Agent runtime 的 prompt construction 規則被組合起來。當多份指令被組合後，LLM 如何處理互相衝突的指令，屬於模型的 instruction-following 行為。由 Agent runtime 定義的 instruction precedence 負責處理衝突，而非單純仰賴模型的物理特性。

### 2.4 Skills（技能擴充）

- **特性**：具備特定目的的工具操作手冊或工作流腳本。
- **處理語意**：**Shadowing (遮蔽) + Progressive Disclosure (漸進式揭露)**。
- **規則**：
  - **Shadowing**：當 Project 存在與 Global 同名的 Skill 時，Project Skill 將完全遮蔽（Shadow）Global Skill，只有 Local 版本會被載入。
  - **Progressive Disclosure**：**Instructions are persistent context; Skills are discoverable capabilities.** 系統啟動時只向模型暴露技能的名稱與描述 (Discovery)；直到模型判斷需要使用時，才動態載入完整的 `SKILL.md` 內容。

---

## 3. Agent Comparison (常見代理實作比較)

### 3.1 Antigravity CLI (agy) & Antigravity IDE

Antigravity 是 Google 推出的 AI-first 開發平台 (官方網站：`https://antigravity.google/`)。CLI 與 IDE 共享相同的核心引擎與客製化生態系統，擁有極為嚴謹的階層式架構，完美體現了上述語意。

- **Runtime Config**：
  - **Global**：`~/.gemini/config/` (包含內部定義的 JSON 與全域 rules)。
- **Instructions (Context/Rules)**：
  - 採用多層次的 Hierarchical Discovery 機制。
  - **Project**：專案根目錄的 `GEMINI.md`、`AGENTS.md`。
  - **Directory**：位於 `.agents/rules/*.md` 內的檔案（會從當前開啟/編輯檔案的目錄往上尋找至 Git Root）。
  - **行為**：透過 Progressive Disclosure，系統會對檔案進行 Deduplication，並將找到的 Rules 疊加 (Concatenate) 至模型上下文中。
- **Skills (Plugins/Sidecars/Hooks/MCP)**：
  - **User**：`~/.gemini/skills/` 或 `~/.agents/skills/`
  - **Workspace**：`.gemini/skills/` 或 `.agents/skills/`
  - **行為**：採用 Shadowing 與 Progressive Disclosure。若發生同名衝突，Workspace Skill 會遮蔽 User Skill；未主動呼叫前僅載入 Skill Name 與 Description。

### 3.2 Claude Code

Claude Code 結合了 JSON 設定檔與 Markdown 記憶指令。

- **Runtime Config**：`~/.claude/settings.json`（全域）與 `.claude/settings.json`（專案）。
- **Instructions (Context)**：`~/.claude/CLAUDE.md`（全域）與專案根目錄的 `CLAUDE.md`。_(註：早期曾支援 `./CLAUDE.local.md` 作為 Project-local memory，但現已被官方標示為 Deprecated)_
- **Skills**：新版統一放置於 `~/.claude/skills/` 與專案的 `.claude/skills/` 中。

### 3.3 Hermes Agent

Hermes Agent 強烈依賴薄入口與動態路由。

- **Runtime Config**：通常透過 `config.yaml` 或環境變數定義。
- **Instructions (Context)**：
  - **Global Personality**：獨立載入 `~/.hermes/SOUL.md` 或 `$HERMES_HOME/SOUL.md`。
  - **Project Context Type Selection (First-match-wins)**：對入口檔案類型採短路求值：`.hermes.md` → `AGENTS.override.md` → `AGENTS.md` → `CLAUDE.md` → `.cursorrules`，第一個命中的類型生效。
  - **Merged Chain**：若選中 `AGENTS.md` 類型，則會沿 Git root → current working directory 建立多層的 merged chain。越深層的檔案越晚出現，內容優先。
- **Skills**：`~/.hermes/skills/` (全域) 與專案級別技能。採用典型的 Progressive Disclosure，模型需先執行 `skill_view` 才會載入完整內容。

---

## 4. Pocket Agent Design (本專案架構設計)

為了確保極致的擴充性與環境隔離，Pocket Agent 的最終架構嚴格遵循以下流向：

```text
                 ┌──────────────────┐
                 │  Runtime Config  │
                 │  deterministic   │
                 │      merge       │
                 └────────┬─────────┘
                          │
                 ┌────────▼─────────┐
                 │   Instructions   │
                 │   concatenate    │
                 │ Global→Project   │
                 │    →Directory    │
                 └────────┬─────────┘
                          │
                 ┌────────▼─────────┐
                 │     Skills       │
                 │ discover → load  │
                 │ Project shadows  │
                 │      Global      │
                 └──────────────────┘
```

### 4.1 指令疊加與組合規則 (Instruction Hierarchy)

1. **載入 Global Instructions** (`~/.pocket-agent/instructions.md`) 作為最底層的基礎常識。
2. **尋找 Project Entry** 採「單一類型短路 (Discovery)」：尋找 `AGENTS.md` 或 `.agents/AGENTS.md` 作為本專案的進入點。
3. **依序疊加 (Concatenate)**：將 Project Entry 附加於 Global Instructions 尾端。若兩者出現衝突，Pocket Agent 不保證單純依靠文字位置產生 deterministic override；如需 deterministic override，應透過 Runtime Config 的明確設定實現。

### 4.2 技能遮蔽與漸進式揭露 (Skill Discovery & Shadowing)

1. **探索 (Discovery)**：同時掃描 `~/.pocket-agent/skills/` (全域) 與 `.agents/skills/` (專案)。
2. **遮蔽 (Shadowing)**：若兩端存在同名技能，專案層級的技能將無條件遮蔽全域技能。
3. **漸進式揭露 (Progressive Disclosure)**：只將過濾後的技能名稱與描述注入 System Prompt。技能本文僅在模型判定需要啟用該技能時，才載入至目前的 Agent Context。
