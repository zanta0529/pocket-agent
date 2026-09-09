# CLI 介面設計檢視與改善建議（更新版）

- 報告日期：2026-09-09（初版）/ 更新：2026-09-09（第二版）
- 專案：Pocket Agent
- 性質：綜合分析報告，含已完成項目追蹤與新增建議。
- 參考工具：Antigravity CLI (`agy`)、Claude Code、Codex CLI、Gemini CLI

---

## 一、本輪已完成的項目

以下項目在第一版報告後均已實作，無需再列入待辦：

| 項目                                          | 實作狀態                                     |
| --------------------------------------------- | -------------------------------------------- |
| 品牌 Banner（口袋造型 ASCII 圖示）            | ✅ 已完成，`cli_render.py banner()`          |
| Banner 先於 MCP 啟動訊息顯示                  | ✅ 已完成，`cli.py` 調整了順序               |
| Banner 全部文字移除 dim 效果                  | ✅ 已完成                                    |
| 使用者輸入訊息以藍色顯示                      | ✅ 已完成，`user()` 套用 `bold blue`         |
| 工具執行顯示彩色標記（`● Read/Write/Action`） | ✅ 已完成，`event()` 新增 `tool_start` 支援  |
| 危險操作圓點紅色、安全操作圓點綠色            | ✅ 已完成，依關鍵字分類                      |
| 動作名稱與路徑之間加入空白                    | ✅ 已完成                                    |
| 路徑字串移除 dim 效果，以正常顏色顯示         | ✅ 已完成                                    |
| Ollama 附加檔案時 400 錯誤修復                | ✅ 已完成，`ollama_engine.py` 轉換訊息格式   |
| `PocketAgent-cli.exe` 分離打包                | ✅ 已完成，`PocketAgent.spec` 新增 `exe_cli` |

---

## 二、現況分析：與 agy 等主流 AI CLI 的差距

### 2.1 與 Antigravity CLI (`agy`) 的對比

Pocket Agent 已在外觀層面與 agy 的設計語言有一定近似度（藍色使用者輸入、黃/紅色的工具執行標記），但以下幾個核心操作體驗仍有明顯落差：

#### ❶ 串流輸出（Streaming）

**agy 的做法：** 回答字元逐一串流顯示，使用者幾乎感受不到等待。

**Pocket Agent 現況：** `send_turn()` 中使用 `console.status("處理中…")` 等待 `agent.chat()` 回傳完整字串後才一次性 `print`。`on_stream` 回呼雖有 `text` 事件，但 `cli.py` 完全未消費此事件，導致：

- 使用者看不到任何逐字進度
- 即使有推理中間輸出（`thinking`），CLI 也完全靜默

**建議：** `send_turn()` 應移除 `with renderer.console.status()` 包裹 `agent.chat()`，改為在 `event()` 處理 `"text"` 事件時直接 `print` 到終端，實現逐字串流。這是操作體驗差異最大、也最應優先實作的項目。

#### ❷ 工具執行中的即時計時顯示

**agy 的做法：** 工具執行旁邊顯示已耗時秒數，如 `● Read (file.py)  2.3s`。

**Pocket Agent 現況：** `tool_start` 只顯示名稱，無執行時間；`tool_done` 事件已從核心發出但 `event()` 完全未處理。

**建議：** 在 `event()` 新增對 `tool_done` 事件的處理，顯示 `✓ 完成 (工具名稱)  x.xs` 形式的結果提示。

#### ❸ 對話歷史（Session）未接入 CLI

**agy 的做法：** `/session` 指令可列出、切換、載入歷史對話。

**Pocket Agent 現況：** `agent/sessions.py` 已有完整的 `save_session / list_sessions / load_session / delete_session` 實作，但 CLI 的 `CommandHandler` 完全沒有接入這些功能。GUI 是否已接入不在本報告範圍，但 CLI 目前是功能空白。

**建議：** 新增 `/session`、`/session list`、`/session load <id>` 等指令，對接 `sessions.py`。

#### ❹ 推理過程（Thinking）未顯示於 CLI

**agy 的做法：** 如果模型有推理過程，會以灰色縮排方式顯示思考段落，協助使用者理解 AI 的決策邏輯。

**Pocket Agent 現況：** `core.py` 確實在 `_run_loop` 中 `self._emit("thinking", event.delta)`，但 `cli_render.py` 的 `event()` 對 `thinking` 事件完全靜默。GUI 有獨立的「思考」面板；CLI 完全無法看到推理過程。

**建議：** 在 `event()` 新增對 `thinking` 事件的支援，以 `dim italic` 縮排顯示，並限制顯示行數避免刷屏。

#### ❺ 缺少執行中止的安全取消機制

**agy / Claude Code 的做法：** 執行中可按 Ctrl+C 中止當前 AI 輪次，但不會退出程式。

**Pocket Agent 現況：** `Agent.cancel_requested` 欄位存在，`Agent.request_stop()` 方法也存在，但 `conversation()` 的 Ctrl+C 只在「等待輸入期間」有效（清空輸入框）。一旦進入 `send_turn()` 的等待，Ctrl+C 就完全無法中止。

**建議：** 應在 `send_turn()` 中，在啟動 `agent.chat()` 的同時設置 Ctrl+C 的處理（捕獲 `asyncio.CancelledError` 並呼叫 `agent.request_stop()`）。

---

### 2.2 功能對比表

| 功能                     | agy | Claude Code | Pocket Agent CLI | 備注                              |
| ------------------------ | :-: | :---------: | :--------------: | --------------------------------- |
| 逐字串流輸出             | ✅  |     ✅      |        ❌        | 回呼已就緒，CLI 未消費            |
| 工具執行時間顯示         | ✅  |     ✅      |        ❌        | `tool_done` 未處理                |
| Ctrl+C 中止當前輪次      | ✅  |     ✅      |        ❌        | `cancel_requested` 存在但未串接   |
| 推理過程（Thinking）顯示 | ✅  |     ✅      |        ❌        | 事件已發出但 CLI 未消費           |
| 對話歷史管理             | ✅  |     ✅      |        ❌        | `sessions.py` 已實作但 CLI 無指令 |
| 工具執行彩色標記         | ✅  |      △      |        ✅        | 本輪已完成                        |
| 使用者輸入藍色顯示       | ✅  |     ❌      |        ✅        | 本輪已完成                        |
| 指令 Tab 補全            | ✅  |     ❌      |        ✅        | 已有 `SlashCommandCompleter`      |
| 路徑 Tab 補全            | ✅  |     ❌      |        ✅        | 已有                              |
| 底部狀態列               | ✅  |     ❌      |        ✅        | 已有                              |
| 附件管理（查看/移除）    | ✅  |     ❌      |        ✅        | `/attachments` 指令               |
| 模型互動式切換           | ✅  |     ❌      |        ✅        | `/model` + `reader.choose()`      |
| 沙盒執行                 |  △  |     ❌      |        ✅        | WSL/Docker sandbox                |
| MCP 整合                 | ✅  |     ✅      |        ✅        |                                   |
| Plugin 系統              | ✅  |     ❌      |        ✅        | `plugins.py`                      |
| 子代理 (Subagents)       | ✅  |     ❌      |        ✅        | `subagents.py`                    |
| 事件鉤子 (Hooks)         | ✅  |     ❌      |        ✅        | `hooks.py`                        |
| GUI / CLI 雙模式         | ❌  |     ❌      |        ✅        | 本專案獨有優勢                    |

---

## 三、分層改善建議

### 第一優先：核心體驗（高衝擊、低風險）

#### 1. 實作 CLI 串流輸出（最高優先）

這是操作感受差異最大的一項。

**實作策略：**

```python
# cli_render.py - 新增 stream_start / stream_end 狀態追蹤
class CliRenderer:
    def __init__(self, console):
        self.console = console
        self._streaming = False  # 串流中時不能重繪

# event() 新增對 text 事件的處理：
elif kind == "text":
    if not self._streaming:
        # 第一個 text 事件：先印 "● Pocket Agent" 標頭
        self.console.print(Text("● Pocket Agent", style="bold #B8860B"))
        self._streaming = True
    self.console.print(data, end="", highlight=False)
```

**相應地，`send_turn()` 需要移除 `with renderer.console.status("處理中…")` 的包裹。**

#### 2. 接入執行中止（Ctrl+C）

```python
# cli.py send_turn() 改寫思路
async def send_turn(...):
    task = asyncio.create_task(agent.chat(result.prompt, attachments=snapshot))
    try:
        _, reply, _ = await task
    except asyncio.CancelledError:
        agent.request_stop()
        renderer.message("⏹ 已停止", "yellow")
        return
```

同時在 `conversation()` 的 Ctrl+C 處理中，偵測若正在執行任務則呼叫停止，而非只印提示訊息。

#### 3. 處理 `tool_done` 事件

在 `event()` 新增：

```python
elif kind == "tool_done":
    if isinstance(data, dict):
        name = data.get("name", "")
        ok = data.get("ok", True)
        status_icon = "✓" if ok else "✗"
        status_color = "green" if ok else "red"
        self.console.print(Text(f"  {status_icon} {name}", style=status_color))
```

---

### 第二優先：對話操作體驗

#### 4. 接入 CLI 對話歷史管理

`sessions.py` 已有完整實作，只需在 `CommandHandler` 新增以下指令：

| 指令                   | 說明                               |
| ---------------------- | ---------------------------------- |
| `/session`             | 列出最近對話（同 `/session list`） |
| `/session list`        | 列出最近 10 筆對話，顯示 ID 與標題 |
| `/session load <id>`   | 載入歷史對話，恢復 `agent.history` |
| `/session save`        | 手動存檔（正常結束時應自動存）     |
| `/session delete <id>` | 刪除歷史對話                       |

同時建議 `_run_cli()` 結束前自動呼叫 `save_session()`，對齊 agy 的行為。

#### 5. 顯示推理過程（Thinking）

在 `event()` 新增：

```python
elif kind == "thinking":
    # 不要全量顯示，只顯示首行或摘要
    snippet = str(data).strip().split("\n")[0][:80]
    if snippet:
        self.console.print(Text(f"  ∴ {snippet}…", style="dim italic"))
```

---

### 第三優先：資訊可見性提升

#### 6. 歡迎畫面新增「入門提示」

對齊 agy 在空白狀態顯示示範輸入的設計。在 `welcome()` 中新增建議輸入範例：

```
/ 指令 · @ 檔案 · Tab 補全 · Enter 送出 · Esc 後 Enter 換行

試著輸入：
  @README.md 幫我整理使用方式
  /session list 查看歷史對話
```

#### 7. 工具日誌輸出干擾問題

**現況問題：** `agent.log` 中的 `[INFO] Using OllamaChatEngine for gemma4:e2b` 等訊息直接寫入 `sys.stderr` 並顯示在終端，與 `處理中…` spinner 交錯，視覺混亂。

**建議：** 在 CLI 啟動時，把 `pocket-agent` 的 `StreamHandler` 過濾掉或提升其等級至 `WARNING`，確保只有使用者操作相關訊息顯示於終端，詳細 log 僅寫 `agent.log`。

---

## 四、現有優勢，不應輕易改動

Pocket Agent 相比 agy 等工具，有幾個值得保留的獨特優勢：

1. **GUI / CLI 雙模式**：對非技術使用者有極大意義，應持續投資。
2. **Plugin zip 系統**：一鍵安裝 Skill + MCP 配置，是非常務實的分發機制。
3. **事件鉤子（Hooks）**：`before_tool / after_tool` 鉤子允許企業注入審計、安全過濾，是 agy 目前沒有的能力。
4. **Shell Sandbox（WSL/Docker）**：對企業內網部署場景是重要的安全機制。
5. **OAuth PKCE 直連**：免 API Key 的 ChatGPT Plus 串接，是本工具的核心競爭差異。

---

## 五、快速行動清單

按優先順序列出可獨立實作的改善項目：

- [ ] **P0** `cli.py` 移除 `console.status()` wrapper，改用 `event(text)` 串流顯示回答
- [ ] **P0** `cli_render.py event()` 新增 `text` / `round_start` 事件處理
- [ ] **P1** `cli.py send_turn()` 接入 Ctrl+C 中止機制
- [ ] **P1** `cli_render.py event()` 新增 `tool_done` 處理，顯示 `✓/✗` 結果
- [ ] **P1** `cli_commands.py` 新增 `/session` 指令族群，對接 `sessions.py`
- [ ] **P2** 自動在 CLI 對話結束時呼叫 `save_session()`
- [ ] **P2** `cli_render.py event()` 新增 `thinking` 事件縮排顯示
- [ ] **P2** `welcome()` 新增入門示範提示
- [ ] **P3** 過濾 `StreamHandler` 的 INFO log，避免干擾終端輸出
- [ ] **P3** Banner 考慮加入目前版本號（VERSION 檔）

---

_本文件為設計分析，不代表上述功能已實作。實作前請對照當時程式碼確認。_
