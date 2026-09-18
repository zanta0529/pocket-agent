# AI Agent 工具「主代理 / 子代理」權限與上下文機制評估報告

_（主要比較對象：Claude Code（Anthropic）、Codex CLI（OpenAI）、Antigravity CLI（Google）；Hermes Agent（Nous Research）作為補充參考案例，機制真實存在但治理成熟度不列入同等比較）_

---

## 📌 結論建議

| 優先級 | 結論                                                                                                                                                                                                                                                                                                                                                                                                                                      | 時效性                                                       |
| ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| **P0** | 三套主要工具已收斂到同一套設計語言：**工具範圍（tool scope）控制 + 執行沙盒（sandbox）控制 + 上下文隔離（fresh context）**三層疊加，不再有「全有或全無」授權。差異主要在「用什麼粒度描述權限」。                                                                                                                                                                                                                                          | 目前（2026 年 9 月）版本，務必留意各家版本迭代快、欄位常改名 |
| **P0** | **Claude Code 與 Codex 走「工具 / 沙盒雙軌制」**：Claude Code 用 `tools`／`disallowedTools` 白黑名單 + 全域 `permissions`；Codex 用 `sandbox_mode`（read-only／workspace-write／danger-full-access）為主，沒有逐工具白名單。**Antigravity CLI 則是三段式 `allow`／`ask`／`deny` 動作清單**（`read_file`、`write_file`、`command`……），優先序 Deny > Ask > Allow。三者「怎麼形容權限」的哲學不同，遷移時不能直接套用同一份設定檔。         | —                                                            |
| **P1** | 子代理對主代理的「上下文繼承」預設值不同：Claude Code 子代理**預設繼承主線程幾乎全部工具**（除非明確限制）；Codex 子代理**預設繼承父層 sandbox／approval 即時覆寫值**（即使自訂 agent 檔案設了別的值，現場 `/permissions` 或 `--yolo` 仍優先）；Antigravity 子代理走**獨立設定檔**，鎖定與否純粹取決於該 agent 的 `settings.json` 是否帶 `permissions` 區塊。**三者都不是「主代理權限自動降級給子代理」的單一邏輯**，實作前必須逐一查證。 | —                                                            |
| **P2** | Hermes Agent 的 `delegate_task` 機制（`toolsets` 參數限制子代理工具、完全隔離上下文）概念上與前三者一致，可作為「輕量開源對照組」附於報告最後，但企業採用成熟度、文件完整度明顯低於前三者，僅作補充參考，不建議與前三者放在同一治理成熟度層級比較。                                                                                                                                                                                       | —                                                            |

---

## 🧠 分析藍圖（框架與假設）

本報告用四個維度做逐一比對，這是目前業界描述 subagent 治理最常用的切分方式：

1. **工作區／執行環境隔離（Isolation）**：子代理的檔案系統、shell、worktree 是否與主代理分開
2. **權限與授權模型（Authorization）**：誰能核准危險操作、核准規則能否被子代理繞過
3. **可用工具範圍（Tool Scope）**：子代理能拿到哪些工具，粒度多細
4. **上下文管理（Context）**：子代理看不看得到主線程歷史、回傳什麼給主代理

**假設與限制**：以下內容以 2026 年 9 月官方文件與 changelog 為準；三套工具都在快速迭代（尤其 Codex 的 `[agents]` 欄位、Antigravity 的 `/agents` 面板），半年後部分欄位名稱可能已變動，實作前建議重新查證官方文件版本號。

---

## 🔍 模組化執行（分維度分析）

### 一、Claude Code（Anthropic）

- **隔離**：`isolation: worktree` 可讓子代理（含 fork）在暫時 git worktree 中工作，實體隔離主線程 checkout；非 worktree 子代理則與主線程共用工作目錄，但 `cd` 不會影響主線程。
- **授權**：子代理 `permissionMode` 可設 `default`／`acceptEdits`／`auto`／`dontAsk`／`bypassPermissions`／`plan`，但**主線程若處於 `bypassPermissions`／`acceptEdits`／`auto`，子代理一律跟隨主線程模式**，子代理自訂的 `permissionMode` 被忽略（`bypassPermissions` 例外規則另計）。全域 `settings.json` 的 `permissions.deny` 對子代理與主線程同時生效，子代理無法繞過。
- **工具範圍**：`tools`（白名單，留空 = 繼承主線程全部工具）+ `disallowedTools`（黑名單，優先於白名單）。背景執行的子代理另有一份**強制精簡工具清單**（僅 Read、Grep、Glob、Bash、Edit、Write 等固定集合），即使 `tools` 欄位寫了其他工具也會被砍掉。
- **上下文**：子代理**預設從零開始**，只拿到系統提示、委派訊息、CLAUDE.md、主線程啟動時的 git snapshot；唯有 **fork**（`/subtask`）例外，繼承主線程完整對話與工具集。子代理逐字稿獨立存檔，不影響主線程壓縮（compaction）。

### 二、Codex CLI（OpenAI）

- **隔離**：官方文件未見獨立 worktree 欄位（不同於 Claude Code），子代理主要靠 `sandbox_mode`（`read-only`／`workspace-write`／`danger-full-access`）做環境層級的讀寫邊界，而非檔案系統實體分割。
- **授權**：**子代理預設繼承當前 sandbox policy 與 approval mode**。關鍵細節：Codex 在產生子代理時，會**重新套用主線程當下的即時覆寫值**（例如互動中改過的 `/permissions` 或 `--yolo`），即使自訂 agent 的 TOML 檔設了不同的 `sandbox_mode` 也一樣——這代表「使用者當場改的權限」優先權高於「開發者寫死的 agent 設定檔」。非互動模式下，子代理若遇到需要新核准的操作會直接失敗回報，而不是掛起等待。
- **工具範圍**：**沒有逐工具白／黑名單**，而是透過 `sandbox_mode` + `mcp_servers`（限定可用的 MCP 伺服器）+ `skills.config` 三個欄位組合出能力邊界。內建三種子代理：`default`（通用）、`worker`（執行導向）、`explorer`（唯讀探索）。自訂 agent 只需 `name`、`description`、`developer_instructions` 三個必填欄位，其餘（model、reasoning effort、sandbox_mode 等）未填即繼承父層。
- **上下文**：子代理在獨立 **agent thread** 中工作，結果以摘要形式回收到主線程，設計目的明確寫在文件裡：對抗「**context pollution／context rot**」（雜訊淹沒決策上下文）。`[agents] max_concurrent_threads_per_session` 控制同時開幾條子代理線程。

### 三、Antigravity CLI（Google）

- **隔離**：桌面版 Antigravity 2.0 可跨 workspace／worktree 調度**並行本地子代理**；CLI 版本（`agy`）本身提供 OS 核心層級沙盒（`enableTerminalSandbox`，類似 Claude Code 的 Seatbelt／Landlock 機制），預設關閉。
- **授權**：**三段式清單制**——全域 `settings.json` 定義 `allow`／`ask`（預設）／`deny` 三個列表，每條規則格式為 `action(target)`（例如 `command(git)`、`read_file(...)`）。**優先序固定為 Deny > Ask > Allow**，無法用子代理設定反轉。支援的 action 類型：`read_file`、`write_file`、`read_url`、`execute_url`、`command`、`unsandboxed`、`mcp`。預設保護規則：當前工作區內的檔案讀寫自動放行，其餘未設定的操作一律視為 `ask`。另有四種權限模式可整體切換：`request-review`、`always-proceed`、`strict`、`proceed-in-sandbox`。
- **工具範圍**：沒有查到與 Claude Code 對等的逐工具白名單欄位，權限控制的最小單位是「動作類型 + 目標」而非「工具名稱」，這點與前兩者的設計哲學明顯不同。
- **上下文**：`/agents` 面板管理執行中／已完成的子代理，子代理可平行執行並各自請求核准。**需注意**：官方文件明確寫「是否鎖定純粹取決於該 agent 自己的 `settings.json` 是否帶 `permissions` 區塊」——換句話說，子代理的權限繼承邏輯偏向「每個 agent 自帶一份設定」而非「自動從父層繼承後裁切」，這點與 Claude Code、Codex 都不同，建議實作前務必用小規模測試驗證實際繼承行為，官方文件在這塊的敘述相對簡略。

### 四、Hermes Agent（Nous Research）——補充參考案例

- **隔離**：`delegate_task` 建立的子代理是**全新的 AIAgent 實例**，擁有獨立對話上下文與獨立 terminal session，主代理被中斷會連帶取消所有子代理並丟棄其工作成果（無斷點續傳）。
- **授權／工具範圍**：核心特色是 `toolsets` 參數——每個子代理拿到的工具集合由父代理**明確指定**（例如研究型子代理只給 `["web"]`，不給檔案或終端機工具），官方文件強調這是「執行期強制」而非僅靠提示詞約束，設計理念與 Claude Code 的 `tools` 白名單相近。
- **上下文**：子代理**完全從零開始**，不繼承任何父層對話，可設定巢狀委派深度（spawn depth）建立編排階層（orchestrator hierarchy）。
- **定位**：這套機制真實存在且設計嚴謹，但 Hermes 本質是個人／研究向的開源自我進化助理，企業級治理案例（稽核、合規、多租戶隔離）遠不如前三者成熟，僅供架構設計參考，不建議與 Claude Code、Codex、Antigravity 放在同一治理成熟度層級比較。

---

## 💡 整合洞察（可執行建議）

1. **不要用同一份心智模型套用三套工具**：Claude Code 是「工具粒度」思維，Codex 是「沙盒粒度」思維，Antigravity CLI 是「動作 + 目標粒度」思維。若貴公司要跨工具制定治理政策，建議先抽象出「唯讀／可寫本地／可寫 + 可執行／無限制」四個通用等級，再映射到各工具的實際欄位，而不是逐欄位硬翻譯。
2. **「使用者當場覆寫」在 Claude Code 與 Codex 都優先於「開發者寫死的子代理設定」**——這是一個容易被忽略的安全考量：如果主線程處於高權限模式（`bypassPermissions`／`--yolo`），即使你在子代理定義檔裡寫了唯讀限制，實際執行時仍可能被主線程狀態覆蓋。**「唯讀子代理」的保護力上限取決於主線程當下的權限模式，不是絕對防線。**
3. **子代理的上下文隔離主要目的是「防污染」而非「安全」**：三家官方文件不約而同強調子代理隔離是為了避免 context pollution／context rot，安全邊界主要還是靠 sandbox／permission 層處理，這是概念上值得區分的兩件事。

---

## ✨ 創意重構（反向視角）

若反過來問：「這麼精細的子代理權限設計，對多數團隊是否真的必要？」——三家官方文件都特別強調子代理**會消耗更多 token／成本**（尤其 Codex 明文警告），且權限模型越細，設定檔維護成本越高、越容易出現「以為鎖住了但其實沒鎖住」的假象（如上述 Claude Code 的模式覆寫問題）。對於團隊規模小、任務單純的場景，**過度設計子代理權限矩陣本身就是一種風險**——不如先用最保守的整體沙盒等級（如 Codex 的 `read-only` 全域預設），等真的出現需要平行探索／大量輸出的任務類型，再逐步引入細粒度子代理設定，而不是一開始就打造一套完整的白黑名單治理框架。

---

## ⚠ 限制邊界（時效與假設）

- 本報告基於 2026 年 9 月官方文件與 changelog，三套工具皆在快速迭代（Codex 的 `[agents]`、Antigravity 的 `/agents`／`settings.json` 欄位近期都有變動紀錄），**實作前務必重新查證當下版本文件**，不要直接套用本報告的欄位名稱。
- Antigravity CLI 子代理的「權限繼承邏輯」官方文件敘述相對簡略，本報告的判讀（每個 agent 自帶設定而非自動裁切繼承）建議用小規模實測驗證，不宜直接視為最終定論。
- Hermes Agent 屬社群驅動的開源專案，其 `delegate_task`／`toolsets` 機制雖有官方文件佐證，但版本間行為變動快（有社群回報過模型覆寫失效等 bug），企業採用前建議自行做 POC。

---

## Rethink（第三方檢視）

1. **對照維度可再擴充**：本報告聚焦「權限／工具／上下文」三軸，若要支援實際選型決策，建議再補一欄「稽核與可觀測性」（subagent output scanning、逐字稿留存政策），三家在這塊的深度差異其實不小（Claude Code 有明確的 subagent output scanning 機制對抗提示注入）。
2. **成本維度未量化**：三家都提到子代理增加 token 消耗，但沒有具體數字對照，若這份報告要用於預算評估，建議另外做一次實測基準測試（benchmark）。
3. **Antigravity 資訊時效風險較高**：Antigravity CLI 是 Gemini CLI 的後繼產品（2026 年 5 月才宣布取代），文件與功能仍在快速補齊，三者中此工具的資訊穩定性相對最低，引用時建議額外註明查證日期。
4. **未涵蓋企業級稽核合規面**：如需要用於實際採購或架構決策，建議再補查三家在 SOC2、資料落地、稽核留存等合規面向的官方承諾，本報告未觸及。
