---
title: '排程作業文件'
project: 'PocketAgent'
version: '0.1.0'
date: '2025-01-20'
language: 'zh-TW'
---

# PocketAgent 排程作業文件

本文區分 CLI 啟動時的背景作業、互動式維護與 GitHub Actions 事件。以下為靜態查核結果；執行時間、重試行為與現場告警未經實測。

## 1. 作業總覽

| 作業 | 類型 | 觸發條件 | 來源 |
| --- | --- | --- | --- |
| Housekeeping | CLI 背景作業 | 啟動時檢查是否啟用及是否已過冷卻期；`/clean` 可手動執行 | [CLI 啟動流程](../../src/pocket_agent/frontend/cli/main.py)、[Housekeeper](../../src/pocket_agent/system/housekeeping.py) |
| Portable 自動更新準備 | CLI 背景作業 | Windows Portable 啟動且 `system.auto_update` 啟用 | [CLI 啟動流程](../../src/pocket_agent/frontend/cli/main.py)、[UpdateManager](../../src/pocket_agent/system/updater.py) |
| 會話修剪 | 手動維護 | `/prune` 選擇條件並確認 | [會話指令](../../src/pocket_agent/frontend/cli/commands/session.py) |
| MCP 連線參數解析 | 隨工具呼叫處理 | 已設定的 MCP 工具有連線參數需求 | [MCPManager](../../src/pocket_agent/extensions/mcp.py) |
| CI／發布 | GitHub 事件 | CI 支援手動觸發及 `main`、`dev` 的 push／PR；發布由 `v*` tag 的 push 觸發 | [CI](../../.github/workflows/standard-ci.yml)、[發布](../../.github/workflows/release.yml) |

RHEL 的 cron／systemd timer 探查屬於[遠端唯讀工具](../../mcp_servers/rhel_mcp.py)，不替 PocketAgent 建立排程。上述 CLI 作業與所查閱的 Workflow 未顯示 `cron` 或 Windows Task Scheduler 註冊流程；實際部署端是否另行配置，未查證。

> **架構師風險評估**：24 小時是冷卻門檻，不是固定執行週期。若 CLI 持續開啟或長時間未啟動，不能據此承諾每日維護。CI 與發布同樣不提供定時健康檢查。

## 2. 作業詳細規格

### 2.1 Housekeeping 自動管家

- CLI 啟動後透過 `asyncio.create_task` 搭配 `run_sync` 呼叫 `run_cleanup(force=False)`。停用或仍在冷卻期時會略過；`/clean` 呼叫 `force=True`，不受兩項條件限制。
- 冷卻期預設為 24 小時。時間戳通常存於使用者目錄的 `cache/last_housekeeping.timestamp`，無法建立快取目錄時回退至 scratch；不是 `config.json` 欄位。讀取錯誤或格式無效視為不在冷卻期，寫入失敗記錄 debug 訊息。
- 清理範圍包括舊版根目錄日誌、暫存輸出、剪貼簿、研究檢查點、研究筆記、工作區日誌及備份，也涵蓋其他已管理工作區及程式指定的回退目錄。預設 TTL 為 7 天，日誌與活躍備份分別為 14 天及 28 天。暫存預設配額為每目錄 100 個檔案及 50 MB；備份容量預設上限為 200 MB。配額不是掃描逾時。
- 有清除檔案才記錄完成的 info 訊息；`/clean` 另回報清除容量或剩餘狀態。參閱 [Housekeeper](../../src/pocket_agent/system/housekeeping.py)、[預設常數](../../src/pocket_agent/system/constants.py)、[手動維護指令](../../src/pocket_agent/frontend/cli/commands/misc.py)與[設定規格](../guides/configuration-specification.md)。

> **架構師風險評估**：程式內的鎖不代表跨多個 CLI 程序互斥。時間戳寫入失敗可能讓後續啟動再次清理；保留期也不等於備份策略，不應在 scratch 保存唯一副本。

### 2.2 Portable 自動更新準備

- 支援條件為 Windows 及 Portable，`system.auto_update` 預設啟用。CLI 背景呼叫 `download_and_stage()`，成功時填入待更新的短 commit SHA；此背景呼叫本身不直接套用更新。
- 更新器依 `system.update_repo`、`system.update_branch` 或建置資訊查詢 GitHub 上的目標分支，再以 commit SHA 取得來源封存。這與由 `v*` tag 建置的 GitHub Release 不同。
- CLI 背景例外記在 debug，離開時會對未完成的背景任務提出取消。手動操作與回滾見[運維手冊](../guides/runbook-generated.md)。來源：[CLI 啟動流程](../../src/pocket_agent/frontend/cli/main.py)、[更新器](../../src/pocket_agent/system/updater.py)。

> **架構師風險評估**：分支會移動，版本號也可能不變。要求固定版本的部署宜使用核准的 Release，並用 commit SHA 區分實際執行的建置。

### 2.3 Session Prune 手動清理（`/prune`）

`/prune <days>` 與 `/prune --keep <count>` 是兩種擇一的規則；未提供參數時，選單提供 30 天或保留 20 筆的預設選項。程式先列候選，取得確認後才刪除，並排除目前會話、回報刪除筆數與釋放空間。參閱[指令流程](../../src/pocket_agent/frontend/cli/commands/session.py)與[會話持久化](../../src/pocket_agent/core/sessions.py)。

> **架構師風險評估**：保留 20 筆不是套用天數規則時的額外保護。刪除前先核對規則與候選清單；目前會話排除不代表其他程序開啟的會話也受保護。

### 2.4 MCP 連線處理（非排程）

連線參數解析屬於 [MCPManager](../../src/pocket_agent/extensions/mcp.py) 的工具呼叫流程；依伺服器設定與前端回呼處理，不應解讀為對 SSH、RDP 或資料庫定時探測。設定與權限見 [MCP 架構](mcp-architecture.md)。

> **架構師風險評估**：沒有背景健康指標可用來證明服務可用。排障時應以該次工具結果與允許查閱的日誌為準，不能將失敗視為可以無條件重送操作。

## 3. 監控與可觀測性

手動清理可核對 CLI 回報，自動清理及背景更新可查閱工作區日誌（路徑由[路徑模組](../../src/pocket_agent/system/paths.py)解析），CI 和發布可在 GitHub Actions 查看個別執行結果。未從上述實作確認集中儀表板、固定告警門檻或自動重試服務。若需要監控，請由部署方另行定義「預期執行但未完成」的判定方式。

> **架構師風險評估**：沒有成功日誌不一定是失敗，也可能是停用、冷卻期或沒有檔案可清。調查前先區分這些情況，並在分享日誌前遮罩敏感資料。

## 4. 變更記錄

| 版本 | 日期 | 作者 | 變更摘要 |
| --- | --- | --- | --- |
| 0.1.0 | 2025-01-20 | PocketAgent Team | 初版建立（由 generate-system-docs 自動生成） |
