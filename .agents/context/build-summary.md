# Build Summary

## 任務形狀
- 任務類型：`feature`
- 專案類型：`frontend-only` (GUI + CLI)
- 複雜度：`Full`
- 風險等級：`Medium`

## 實作內容摘要

1. **依賴更新**：
   - 已在 `requirements.txt` 新增使用者核准的 `prompt_toolkit>=3.0.40` 依賴套件。

2. **GUI (CustomTkinter) 實作**：
   - 於 `gui/chat.py` 中新增 `AutocompleteMenu` 類別，繼承自 `ctk.CTkToplevel`，提供無邊框浮動選單功能。
   - 在 `ChatView` 的 `_build` 階段為 `self.input` (CTkTextbox) 綁定鍵盤事件：
     - `<KeyRelease>`：判斷文字，動態讀取 `/` 開頭的字串並查詢可用指令，顯示並過濾選單。
     - `<Up>` / `<Down>`：選單出現時控制上下選取。
     - `<Tab>` / `<Return>`：選單出現時進行指令補完。
     - `<FocusOut>`：隱藏選單。
   - 調整 `_on_enter` 的行為，在選單顯示時優先進行指令選取，而不是送出訊息。

3. **CLI (Terminal) 實作**：
   - 在 `main.py` 的 CLI 模式中，以 `prompt_toolkit.PromptSession` 替換原先的 `rich.prompt.Prompt.ask()`。
   - 實作了 `SlashCommandCompleter`，可以根據目前輸入的內容動態讀取並提供 `/` 指令的下拉提示與描述，大幅提升 CLI 使用體驗。

## 實際使用工具與環境

- `package.json` 或 Node.js 相關 (無，此為 Python 專案)
- 原生檔案工具 `replace_file_content` 與 `write_to_file`。
- 本機 Python 與 Tkinter。

## 驗證與手動測試指令

受限於本機群組原則 (Group Policy) 限制自動測試執行，請將下列指令複製到終端機執行以手動驗證修改成果：

```bash
# 1. 安裝新依賴
pip install -r requirements.txt

# 2. 測試 CLI 模式 (按 Tab 或打 / 會出現補全選單)
python main.py --cli

# 3. 測試 GUI 模式 (在輸入框打 / 會有懸浮選單)
python main.py --gui
```

## API 合約與偏差
無，純前端 GUI / CLI 介面變更，無後端合約調整。

## 建議治理規則
- 考慮將 prompt_toolkit 封裝成統一的 prompt utility 供其他需要互動輸入的元件使用。
