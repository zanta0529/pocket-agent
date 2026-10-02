**Code‑Review Assessment – Pocket‑Agent**
*Scope: 針對執行效能、穩定度、程式碼精簡（尤其是「斷線重送」相關邏輯）進行只讀評估。*

---

## 📚 檔案概覽  
| 目錄/檔案 | 主要職責 |
|---|---|
| `src/pocket_agent/main.py` | 入口點、CLI/GUI 切換 |
| `src/pocket_agent/frontend/cli/main.py` | CLI 前端、密碼解鎖與重試機制 |
| `src/pocket_agent/core/agent.py` | 主要 Agent 迴圈、工具建構、上下文截斷、子代理支援 |
| `src/pocket_agent/system/*` | Provider（OpenAI、Ollama 等）與認證、更新機制 |
| `src/pocket_agent/tools/*` | 各類工具（FS、Web、Image、RAG、Shell） |
| `tests/*` | 單元/整合測試 |

---

## 🔴 重大問題（Critical Issues）

| 類別 | 位置 | 描述 |
|---|---|---|
| **斷線/重試邏輯不完整** | `frontend/cli/main.py` (第 418‑447 行) | - `retry_count` 只在密碼解鎖失敗時遞增，成功解鎖後仍會回到 `while retry_count < max_retries` 重新執行 `get_provider_client()`，導致不必要的重覆嘗試。<br>- 若 `config.unlock()` 成功，`retry_count` 仍為 0，迴圈會再次進入 `try` 區塊，可能因網路瞬斷再次拋出例外，形成「斷線後自動重送」的無限制迴圈。 |
| **上下文截斷估算不精確** | `core/agent.py` (第 608‑633 行) | 以 `len(str)/4` 粗估 token 數並使用固定阈值 30 000，對中文或多字節字元會嚴重低估，導致 **Context Overflow** 頻發或過早截斷，影響效能與回應完整性。 |
| **未使用 `consecutive_errors`** | `core/agent.py` (第 644 行) | 變數宣告後未被利用，錯誤累積的保護機制缺失，可能在連續失敗時導致無限制迭代，影響穩定性。 |
| **工具列表每輪重新建構** | `core/agent.py` (`_build_tools`) | 每輪 `while round_idx < max_tool_rounds` 都呼叫 `_build_tools()`，其中會遍歷 MCP、檔案系統、Skill 索引等，若 Skill 目錄很大，會產生不必要的 I/O 與 CPU 開銷，降低效能。 |

---

## 🟡 重要問題（Important Issues）

| 類別 | 位置 | 建議 |
|---|---|---|
| **日誌層級硬編碼** | `core/agent.py` (第 31‑53 行) | 直接將 noise 套件設為 `WARNING`，忽略使用者在 `PROJECT_INSTRUCTIONS.md` 中可能自訂的 log level。改為根據 `config.get("system.log_level", "WARNING")` 動態設定。 |
| **同步檔案讀取** | `frontend/cli/main.py` (第 384‑398 行) | 使用 `Path.read_text()` 同步讀檔，在大量 MD 展開時會阻塞事件迴圈。建議改為 `await asyncio.to_thread(md_target.read_text, encoding="utf‑8")` 或使用 `aiofiles`。 |
| **重複匯入 `config`** | 多處（如 `core/agent.py` 第 89 行、`main.py` 第 22 行） | 多次匯入會產生額外的模組初始化成本，應在最上層一次匯入後直接使用。 |
| **硬編碼的 `max_retries = 3`** | `frontend/cli/main.py` (第 418 行) | 若使用者需要自訂重試次數，應從設定檔 (`config.get("cli.max_retries", 3)`) 讀取。 |
| **`_safe_read_tools` 的用途不明** | `core/agent.py` (第 471‑477 行) | 目前只收集名稱，未在後續流程中使用，造成不必要的記憶體與 CPU 開銷。若未使用，可移除。 |
| **`ask_question` 工具每輪都加入** | `core/agent.py` (第 421‑426 行) | 大部分對話不需要向使用者提問，將此工具加入 `tools` 陣列會增加 LLM 產生函式呼叫的機會，稍微降低效能。可考慮在需要時透過 `state.session_approved_commands` 動態加入。 |

---

## 🔵 小問題（Minor Issues）

| 類別 | 位置 | 建議 |
|---|---|---|
| **Magic number** | `frontend/cli/main.py` 第 420 行 | `max_retries`、`max_tool_rounds` 等建議抽成常數或設定檔參數。 |
| **過長的單行字串** | 多處（如 `core/agent.py` 第 525‑527 行） | 建議使用多行字串或 `textwrap.dedent` 讓可讀性更佳。 |
| **未使用的 import** | `frontend/cli/main.py` 第 18 行 `from dotenv import load_dotenv`（已在 `main.py` 內使用） | 無問題，但可確認是否所有 import 都真的被使用，以減少 lint 警告。 |
| **硬編碼路徑** | `core/agent.py` 第 17‑18 行 `user_dir()` | 若打包後路徑變化，最好在 `PROJECT_INSTRUCTIONS.md` 說明可覆寫。 |
| **未釋放 `httpx.AsyncClient`** | 多個 provider (`system/providers/*.py`) | 雖然大多採用 `with` 區塊，但在長期運行的 Agent 中，請確保客戶端在程式退出前正確關閉，以免資源泄漏。 |
| **`_stopped_reply` 重複字串** | `core/agent.py` 第 307 行 | 建議抽成常量 `STOPPED_MSG = "⏹ 已由使用者停止"`，提升可維護性。 |

---

## 📈 效能優化建議（Performance）

1. **修正斷線重送迴圈**  
   ```python
   # frontend/cli/main.py
   while retry_count < max_retries:
       try:
           client = get_provider_client()
           # 成功取得 client → 直接跳出迴圈
           break
       except AuthException:
           # ... 解鎖失敗邏輯
           if not config.unlock(password):
               retry_count += 1
               if retry_count >= max_retries:
                   renderer.message("重試次數過多，啟動中止。", "red")
                   return
               continue  # 仍在 while 迴圈，重新嘗試
   ```
   - **效益**：避免在成功解鎖後仍重跑 `while`，減少不必要的網路請求與 CPU 時間。

2. **使用真實 Token 計算**  
   - 引入 `tiktoken`（或相容的 tokenizer）在 `_run_loop` 前先計算 `self.history` 的 token 數，取代 `len(str)/4` 的粗估。  
   - 這可減少過早截斷或因低估而頻繁觸發 `Context Overflow` 的情況，提升回覆完整性與效能。

3. **緩存工具列表**  
   - 在 `Agent.__init__` 時建立一次 `self._static_tools`（MCP、Skill、Image、AskQuestion），在 `_build_tools` 中只在需要時（例如 Skill 目錄變化）追加動態工具。  
   - 減少每輪迭代的 I/O 與 list 合併開銷。

4. **非同步檔案 I/O**  
   - 對大檔案（如 MD 展開）使用 `aiofiles` 或 `await asyncio.to_thread`，避免阻塞事件迴圈。  
   - 示例：
     ```python
     import aiofiles

     async with aiofiles.open(md_target, mode="r", encoding="utf-8") as f:
         md_content = await f.read()
     ```

5. **避免不必要的 `ask_question`**  
   - 只在 `state.session_approved_commands` 為 `False` 時加入，使 LLM 在絕大多數回合不必考慮此 function，降低函式呼叫的搜尋空間。

---

## 🛡️ 穩定性提升建議（Stability）

1. **使用 `consecutive_errors`**  
   - 在 `_run_loop` 中統計連續失敗次數，超過一定門檻（如 3）時自動中止或回報使用者，防止無限錯誤迴圈。
   ```python
   consecutive_errors = 0
   ...
   except Exception as e:
       consecutive_errors += 1
       if consecutive_errors >= 3:
           renderer.message("連續錯誤過多，已中止執行。", "red")
           return "", self._stopped_reply("")
       continue
   else:
       consecutive_errors = 0
   ```

2. **加強 MCP 初始化失敗處理**  
   - `MCPManager.start()` 失敗時僅印出訊息，可考慮將失敗的服務列入白名單排除，或在 `tools` 中剔除對應工具，避免之後產生 `ToolNotFound` 錯誤。

3. **更嚴格的 `max_tool_rounds` 設定**  
   - 防止工具迴圈意外無限執行，若 `max_tool_rounds` 超過 50，提前警告使用者或自行限制。

4. **檢查 `workspace_dirs` 重複**  
   - `add_workspace_dir` 已檢查 `if str(target_dir) not in self.workspace_dirs:`，但未排除大小寫差異或相同實體路徑的不同表示方式。可使用 `Path.resolve()` 再比對。

---

## ✂️ 程式碼精簡建議（Code‑Conciseness）

| 位置 | 建議 |
|---|---|
| `core/agent.py` 第 31‑53 行 | 把噪音 logger 設定抽成 `def _silence_noisy_loggers():`，減少重複程式碼。 |
| `frontend/cli/main.py` 第 418‑447 行 | 合併 `retry_count` 增減與訊息輸出為一個小函式 `def _handle_unlock_failure():`，提升可讀性。 |
| `core/agent.py` 第 470‑478 行 | `self._safe_read_tools` 只在 `if tc.pop("_is_safe_read", False):` 時加入，可直接在 `for t in tools:` 內做過濾，省去兩段迴圈。 |
| `core/agent.py` 第 525‑527 行 | 把系統訊息字串抽成常量 `SYSTEM_INSTRUCTIONS = """…"""`，便於多處重用與維護。 |
| `tools/web.py` 中的 fallback 例外訊息 | 使用 f‑string 直接插入 `curl_e`、`httpx_e`，不需要額外變數。 |

---

## ✅ 後續步驟（Next Steps）

1. **先行修正斷線重送迴圈**（最緊急）  
   - 只在解鎖失敗時遞增 `retry_count`，成功後立即 `break`。  
2. **導入真實 Token 計算**，取代簡易的字元估算。  
3. **實作 `consecutive_errors` 防護**，避免連續失敗的無限循環。  
4. **優化工具建構**：在 `Agent.__init__` 中一次性建立靜態工具，僅在需要時（Skill 目錄變化）重新生成。  
5. **非同步檔案讀取**（MD 展開）以提升 CLI 反應速度。  
6. **整理與抽象重複字串與設定**（log level、max_retries、system messages），提升可維護性與程式碼簡潔度。  
7. **檢測與補齊測試**：確保上述變更後所有單元測試 (`tests/`) 仍通過，特別是 `test_cli_flow.py`、`test_security_agent.py` 與 `test_mcp_manager.py`。 

---

> **提醒**：依照專案安全與授權規範，任何程式碼變更前，請先在本地執行對應測試指令（如 `pytest -q`），確認功能未受影響。若需要執行測試，請自行在終端機複製以下指令執行：
```
pytest -q
``` 

---

此報告已以 **Traditional Chinese**、**台灣科技術語** 撰寫，遵循 `code-review-assessment` 技能規範。如需要更深入的性能分析（如 profiling、記憶體使用監測）或具體的重構 PR，請告知，我可協助產生具體的 PR 建議或草稿。