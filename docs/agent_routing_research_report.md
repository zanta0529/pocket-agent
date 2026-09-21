# 主流 AI 開發代理：路由與上下文管理研究報告

在面對複雜的軟體工程任務（如大範圍重構、全域除錯、跨檔案溯源）時，AI 代理往往會遇到「模型上下文超載 (Context Exceeded)」與「任務迷失」的雙重挑戰。

本報告深入剖析了目前業界主流的四款 AI 開發代理（Antigravity CLI、Hermes Agent、Claude Code、Codex CLI），研究它們如何透過「路由策略」、「工具委派」與「上下文管理」來解決此問題。

---

## 1. Antigravity CLI (Google) 
**核心概念：原生子代理 (Native Subagents) 與反應式平行調度**

Antigravity 採用了標準的 **Supervisor / Worker（主管與工作者）架構**，其底層將子代理（Subagents）與背景任務（Tasks）視為一等公民。

* **顯式委派與隔離**：當主代理面臨需要大量爬梳文件或探索程式碼的任務時，它能透過 `invoke_subagent` 呼叫如 `Researcher` 等專職子代理。子代理在獨立的對話脈絡（Conversation Context）中運作，其產生的龐大日誌與錯誤不會污染主代理的上下文。
* **反應式喚醒 (Reactive Wakeup)**：不同於傳統迴圈輪詢 (Polling) 的浪費，Antigravity 的主代理在委派任務後會進入休眠。當子代理完成工作或發生錯誤時，系統會透過事件驅動自動喚醒主代理。
* **混合策略**：主代理仍保留直接讀寫檔案的能力。由 LLM 透過系統提示 (System Prompt) 自主判斷是「親自執行」還是「委派」。

## 2. Hermes Agent
**核心概念：路由優先 (Router-First) 與能力抽象 (Capability Abstraction)**

Hermes 的設計哲學偏向於「輕量化入口」與「高度跨平台相容」，非常強調專案層級的規範路由。

* **薄入口路由 (Thin Entrypoint Routing)**：依賴目錄結構中的約定檔案（如 `.agents/AGENTS.md` 或根目錄的 `AGENTS.md`）作為路由器。當任務啟動時，它強制要求代理先讀取這些入口，並由入口導向真正的通用規範（如 `AI_INSTRUCTIONS.md`）。這是一種「由系統環境決定代理行為」的路由機制。
* **中立的技能合約**：在面對子任務委派時，Hermes 偏向定義「能力（Capabilities）」而非綁死特定工具 API。例如，它會要求「使用當前平台最強的搜尋能力」，而不是硬編碼呼叫特定的 `grep` 或 `find_by_name`，藉此實現代理間的無縫接軌。

## 3. Claude Code (Anthropic)
**核心概念：巨量上下文 (Giant Context) 與隱式委派 (Implicit Delegation)**

相較於切分出明確的「子代理」，Claude Code 更依賴 Anthropic 模型本身強大的長文本處理能力（200K+ Tokens）與極強的邏輯推理能力。

* **單一巨型脈絡**：Claude Code 傾向於在一個龐大的會話中，利用連貫的思路鏈 (Chain of Thought, CoT) 處理所有事情。
* **工具驅動的「翻頁」**：當遇到上下文極限時，它並不依賴呼叫新代理，而是高度依賴其內建 `read_file` 工具的截斷 (Truncation) 與分頁機制。模型會像人類閱讀長篇文件一樣，自主透過 `start_line` 與 `end_line` 進行滾動閱讀，或使用原生的 `bash` / `grep` 進行過濾，將資訊濃縮後再繼續推論。

## 4. Codex CLI / Cursor-style Agents
**核心概念：系統級路由 (System-level Routing) 與 RAG (檢索增強生成)**

這類工具（如 Cursor 的底層機制）將路由與上下文瘦身的重責大任從 LLM 轉移到了「本地 IDE 系統」上。

* **意圖與檢索路由**：當使用者下達指令時，系統前端會先進行意圖識別。系統會自動執行 Codebase Indexing（如 FTS5 全文檢索或向量檢索），將龐大的專案原始碼切碎。
* **被動接收**：LLM 在這個架構下更像是一個被動接收者。它不需要主動去「委派」搜尋任務，因為系統層面的 Router 已經將最相關的程式碼片段（Context Fragments）準備好並塞入 Prompt 中。這種做法完全避開了 LLM 自行探索可能導致的 Context 爆炸問題。

---

## 綜合比較與決策建議

| 比較維度 | Antigravity | Hermes | Claude Code | Codex / Cursor |
| :--- | :--- | :--- | :--- | :--- |
| **路由決策者** | LLM 判斷 (主代理) | 專案配置 (AGENTS.md) | LLM (CoT 自主決策) | 系統底層 (檢索與意圖) |
| **上下文隔離性** | 極高 (物理隔離的子代理) | 中高 (依賴腳本與規範) | 低 (單一脈絡到底) | 高 (系統篩選送入) |
| **容錯與恢復** | 捕捉錯誤後改變委派策略 | 依循守則停止與回報 | 依賴模型自我糾正 | 依賴使用者介入與重問 |
| **適用場景推薦** | 跨服務除錯、大型重構、需平行處理的探索任務 | 跨平台與高度協作的開源專案 | 高難度邏輯推演、演算法開發 | 快速功能開發、API 使用問答 |

### 結語
在您先前的 `pocket-agent` 日誌中，您見證了類似 **Antigravity** 的容錯與切換機制：當單一工具（或子代理）發生崩潰時，主代理能捕獲錯誤並重新限縮範圍。
理解這些架構後，在開發或配置您的 `pocket-agent` 時，您可以更有意識地運用 `run_subagents_parallel` 來保護主代理的思路清晰，或是強化 RAG 機制來減少不必要的子代理消耗。
