### 🧭 任務形狀

- 任務類型：`feature`
- 專案類型：`frontend-only`
- 複雜度：`Full`
- 風險等級：`Medium`

### 📂 相關檔案與既有模式

- **GUI (`gui/chat.py`)**：目前使用 CustomTkinter 的 `CTkTextbox` 處理輸入，並有實作 `_ime_poll` 等中文輸入法組字救援機制，以及特定的按鍵綁定（如 `Enter` 送出、`Shift+Enter` 換行）。目前沒有現成的浮動選單元件，需自訂基於游標座標彈出的 `CTkFrame` 或 `CTkToplevel`。
- **CLI (`main.py`)**：目前使用 `rich.prompt.Prompt.ask()`，由於 `rich` 的輸入元件是純文字讀取，原生缺乏互動式即時自動補完（Autocomplete）機制，通常需要依賴如 `prompt_toolkit` 等外部庫才能達到現代 CLI 的下拉選單體驗。
- **指令來源 (`agent/commands.py`)**：系統已具備 `expand_command` 與 `get_command` 等 API 來取得指令定義，可作為補完清單的資料來源。

### 📖 使用者故事

- 作為使用者，我想要在 GUI 聊天視窗或 CLI 終端機輸入 `/` 時，能看到可用的 Slash 指令下拉選單或自動補完提示。
- 作為使用者，我想要在繼續輸入文字時，選單能自動過濾匹配的指令（例如輸入 `/h` 過濾出 `/help`），這樣我可以更快速選擇並完成輸入，不必硬記指令名稱。

### ✅ 驗收標準

- **GUI 模式**：
  - 在輸入框鍵入 `/`（且為行首或前有空白）時，彈出指令候選選單。
  - 根據後續輸入的字元即時過濾選單項目。
  - 支援使用上下方向鍵（`Up` / `Down`）選擇選單項目。
  - 支援使用 `Enter` 或 `Tab` 鍵將選定的指令補完至輸入框中。
  - 支援滑鼠點擊直接補完指令。
  - 當刪除 `/` 或輸入框失去焦點時，自動隱藏選單。
- **CLI 模式**：
  - （視待澄清問題決定）在終端機輸入 `/` 時提供指令自動補全機制（例如按下 `Tab` 補完或提供選單）。

### 🚫 不在範圍內

- 新增或修改實際的 Slash 指令後端處理邏輯。
- 將自動補完擴展至 `/` 指令以外的自然語言或一般檔案路徑（除非是指令的參數補完，初期僅限於指令名稱補完）。

### 🔒 不可變條件 / Invariants

- **不得干擾 IME**：不得破壞 `gui/chat.py` 中既有的 Windows 中文輸入法（IME）組字修正與救援邏輯。
- **不得干擾正常發送**：選單隱藏時，`Enter` 必須維持原有的「送出對話」行為；選單顯示時，`Enter` 才能被攔截為「選取指令」。
- **不得影響 `Shift+Enter`**：維持其預設換行行為。

### 🎨 前端規格

**GUI (CustomTkinter) 實作方向**：
1. **輸入綁定 (Key Bindings)**：
   - 擴充 `self.input` (CTkTextbox) 的按鍵綁定，加入 `<KeyRelease>` 事件來偵測目前游標前方的文字，判斷是否處於 `/` 指令觸發狀態。
   - 攔截 `<Up>`、`<Down>`、`<Return>`、`<Tab>`。若補完選單顯示中，優先交由選單處理；否則放行。
2. **浮動選單 (Autocomplete Popup)**：
   - 建立一個繼承自 `CTkFrame` (若在視窗範圍內) 或 `CTkToplevel` (若需跨出主視窗) 的選單元件。
   - 利用 `self.input._textbox.bbox("insert")` 計算當前文字輸入游標的相對座標，來動態決定彈出選單的 (x, y) 位置。
   - 選單內容動態讀取 `agent.commands` 中註冊的指令列表並支援高亮目前選定項目。

**CLI (Terminal) 實作方向**：
- 替換掉原本單純的 `rich.prompt.Prompt.ask()`，引入支援互動式選單的機制（細節取決於是否允許新增依賴）。

### 🧪 驗證策略 / Verification Strategy

- Manual verification (GUI)：
  - 測試在各種游標位置輸入 `/` 的觸發反應。
  - 測試輸入法（如微軟注音、倉頡）開啟時，輸入 `/` 與後續中/英文字的互動是否正常。
  - 測試選單彈出時的方向鍵選擇與 Enter/Tab 補完。
  - 測試滑鼠點擊選單補完。
  - 確認普通對話中的 Enter 傳送功能不被異常攔截。
- Manual verification (CLI)：
  - 確保 CLI 介面載入正常，並能觸發相應的 Slash 指令補全（如 Tab）。

### 📁 預計異動檔案

- `gui/chat.py` (新增綁定與 `AutocompleteMenu` 類別)
- `main.py` (修改 CLI 的 Prompt 邏輯)
- `requirements.txt` (如果授權引入 CLI 補完套件)

### ⚠️ 風險點

- **CustomTkinter 事件衝突**：Tkinter 的按鍵攔截（`return "break"`）若與內部事件或現有 IME 救援事件衝突，可能導致按鍵失靈。
- **游標座標定位**：Tkinter Text 元件在捲動後，`bbox("insert")` 可能回傳 `None` 或不準確，需處理邊界條件確保浮動選單位置正確。

### 🧯 回復策略 / Rollback Notes

- 捨棄對 `gui/chat.py` 及 `main.py` 的變更即可回退。

### ❓ 待澄清問題

1. **CLI 依賴授權**：目前 `main.py` 的 CLI 模式使用 `rich.prompt.Prompt.ask()`，而 `rich` 並不原生支援文字輸入途中的「互動式下拉選單 / 即時自動補完」。若要實作此功能，通常需要引入新的外部依賴（如業界標準的 `prompt_toolkit`）。請問是否核准在 `requirements.txt` 中新增 `prompt_toolkit`？若不核准，CLI 模式的自動補完該作何降級處理（例如忽略 CLI 模式的下拉選單實作，或僅依賴 OS 終端機原生的極基礎功能）？