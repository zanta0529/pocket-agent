---
title: '運維手冊'
project: 'PocketAgent'
version: '0.1.0'
date: '2025-01-15'
language: 'zh-TW'
---

# PocketAgent 運維手冊

> **語系說明**：本文件語系遵循使用者提問語系（預設繁體中文）。若使用者以英文提問，請切換為英文輸出。

---

## 1. 服務概覽

| 項目         | 說明                                                                                     |
| ------------ | ---------------------------------------------------------------------------------------- |
| **服務名稱** | PocketAgent                                                                              |
| **版本**     | 0.1.0                                                                                    |
| **環境**     | 本機 CLI 應用 (Production / Development 同一執行檔)                                      |
| **部署方式** | Modern Portable (推薦) / 原始碼開發模式 / Legacy PyInstaller                             |
| **負責團隊** | PocketAgent 開發團隊                                                                     |
| **值班輪換** | N/A (本機工具，無中央服務)                                                               |
| **文件語系** | zh-TW                                                                                    |
| **最後更新** | 2025-01-15                                                                               |

---

## 2. 部署程序

### 2.1 標準部署流程

```mermaid
flowchart TD
    subgraph Portable["Modern Portable (推薦)"]
        Download[從 GitHub Releases 下載\nPocketAgent-portable.zip]
        Extract[解壓縮至任意目錄\n(如 D:\PocketAgent)]
        RunBat[雙擊 pocket.bat\n或 cmd 執行 pocket.bat]
        Setup[pocket-agent setup\n首次設定精靈]
        Use[開始使用]
    end
    
    subgraph Dev["開發模式"]
        Clone[git clone 專案]
        Venv[python -m venv venv]
        Activate[啟用虛擬環境]
        Install[pip install -r requirements.txt\npip install -e .]
        SetupDev[pocket-agent setup]
        RunDev[run.bat / run.sh / run.command]
    end
    
    subgraph Legacy["Legacy PyInstaller (不推薦)"]
        Build[build.bat 選擇 PyInstaller]
        ExeRun[執行 PocketAgent-cli.exe]
        Risk[防毒誤判風險高]
    end
    
    Download --> Extract --> RunBat --> Setup --> Use
    Clone --> Venv --> Activate --> Install --> SetupDev --> RunDev
    Build --> ExeRun --> Risk
    
    style Portable fill:#e8f5e9
    style Legacy fill:#ffebee
```

| 步驟 | 動作              | 指令/操作                                          | 驗證方式                              | 預估時間 |
| ---- | ----------------- | -------------------------------------------------- | ------------------------------------- | -------- |
| 1    | 下載 Portable 版  | 瀏覽器下載 `PocketAgent-portable.zip`              | 檔案存在、大小約 80-120 MB            | 1-5 分鐘 |
| 2    | 解壓縮            | 滑鼠右鍵「全部解壓縮」或 `tar -xzf`                | 出現 `PocketAgent/` 目錄含 `pocket.bat` | 30 秒    |
| 3    | 啟動              | 雙擊 `pocket.bat` 或 CMD 執行 `pocket.bat`         | 顯示 CLI 介面、版本資訊               | 5 秒     |
| 4    | 首次設定          | 輸入 `setup` 或 `pocket-agent setup`               | 完成 5 步驟精靈，生成 `config.json`   | 2-5 分鐘 |
| 5    | 連線驗證          | 輸入 `/usage` 或任意對話                           | 顯示 Token 用量、模型回應正常         | 10 秒    |

### 2.2 離線/氣隙環境部署

> **適用場景**：企業內網、無外網存取的生產環境、嚴格防火牆環境

#### 2.2.1 Modern Portable 離線部署

```bash
# 線上環境：下載發布包
# 1. 從 GitHub Releases 下載 PocketAgent-portable.zip
# 2. 透過 USB/內網傳輸至離線主機

# 離線環境：解壓縮並使用
# 1. 解壓縮至任意目錄
# 2. 雙擊 pocket.bat 啟動
# 3. 執行 pocket-agent setup 設定

# 注意：離線環境無法使用：
# - ChatGPT OAuth (需瀏覽器存取 auth.openai.com)
# - OpenRouter / Groq / SambaNova / Mistral API (需網路)
# - GitHub MCP (需網路存取 api.github.com)
# - 線上更新 (/update)

# 可用功能：
# - Ollama 本地模型 (http://localhost:11434)
# - 本地檔案操作、Shell、RAG、RepoMap
# - 內建 SQLite/MySQL/MSSQL MCP (需內網資料庫)
# - RHEL/Win RDP MCP (需內網 SSH/RDP)
```

#### 2.2.2 依賴套件離線安裝 (開發模式)

| 生態系    | 離線準備指令 (有網路機器)                              | 離線安裝指令 (目標機器)                                                    |
| --------- | ------------------------------------------------------ | -------------------------------------------------------------------------- |
| **pip**   | `pip download -r requirements.txt -d ./wheelhouse`     | `pip install --no-index --find-links ./wheelhouse -r requirements.txt`     |
| **pip-tools** | `pip-compile requirements.in` → 複製 `requirements.txt` | 同上                                                                       |

#### 2.2.3 Windows 離線部署注意事項

- Modern Portable 內建官方簽章 `pythonw.exe`，**不需要**安裝 Python
- `pocket.bat` 設定 `PYTHONNOUSERSITE=1`、`PYTHONPATH=%~dp0app\src` 完全隔離
- 防毒軟體可能誤判 `pythonw.exe` 子行程，**需加入例外**：解壓縮目錄 `PocketAgent/python/`
- 群組原則封鎖 PowerShell 時，`pocket.bat` 使用 `cmd.exe` 可正常運作

### 2.3 更新與回滾程序

| 觸發條件               | 更新指令         | 驗證方式              | 回滾方式                                      |
| ---------------------- | ---------------- | --------------------- | --------------------------------------------- |
| **Modern Portable**    | CLI 內建 `/update` | 重啟後版本號更新、功能正常 | 刪除 `dist/PocketAgent` 重新解壓縮舊版本 zip |
| **開發模式**           | `git pull` + `pip install -r requirements.txt` | 測試通過、功能正常 | `git checkout <舊commit>` + 重新安裝          |
| **PyInstaller Legacy** | 重新下載新版 exe  | 功能正常              | 保留舊版 exe 覆蓋回來                         |

> **注意**：Modern Portable `/update` 追蹤 GitHub 主分支最新 Commit，非正式 Release。生產環境建議鎖定 Release 版本。

---

## 3. 災難恢復

### 3.1 備份策略

| 資料類型      | 備份頻率                | 保留期限              | 儲存位置                                               | 恢復指令                                      |
| ------------- | ----------------------- | --------------------- | ------------------------------------------------------ | --------------------------------------------- |
| **會話歷史**  | 每輪對話自動存檔        | 依 `/prune` 設定 (預設 30 天/保留 20 筆) | `~/.pocket-agent/sessions/*.json` (含 HMAC-SHA256)    | 複製檔案至同位置；`/session` 載入驗證         |
| **全域設定**  | 手動修改時              | 永久                  | `~/.pocket-agent/config.json`                          | 複製檔案覆蓋；`pocket-agent setup` 重新產生   |
| **MCP 設定**  | 手動修改時              | 永久                  | 專案根目錄 `mcp_config.yaml` / `~/.pocket-agent/mcp_config.yaml` | 複製檔案覆蓋                                  |
| **OAuth Token** | 自動 (Keyring/JWE)    | 永久 (Refresh 自動)   | OS Keyring / `~/.pocket-agent/encrypted_provider` (JWE) | `pocket-agent logout` 後重新 `setup`          |
| **工作區沙盒** | Housekeeping 自動清理  | 預設 7-28 天          | `~/.pocket-agent/workspaces/<slug>/`                   | 通常無需備份；重要筆記請手動複製至專案目錄    |
| **專案原始碼** | Git 提交時              | 永久                  | Git 遠端儲存庫                                         | `git clone`                                   |

### 3.2 恢復時間/點目標 (RTO/RPO)

| 層級         | RTO              | RPO              | 說明                                              |
| ------------ | ---------------- | ---------------- | ------------------------------------------------- |
| **核心功能** | < 5 分鐘         | 0 (即時同步)     | Portable 版解壓縮即用；設定檔手動備份可秒級恢復   |
| **會話歷史** | < 1 分鐘         | 最後一次自動存檔 | JSON 檔案直接複製；HMAC 驗證確保完整性            |
| **MCP 連線** | < 2 分鐘         | 即時 (記憶體快取) | 重新輸入密碼/憑證；Keyring 儲存可自動恢復         |
| **完整環境** | < 10 分鐘        | 最後備份點       | Portable zip + 設定檔 + 會話歷史 完整還原         |

### 3.3 常見災難場景與恢復

| 災難場景                 | 症狀                          | 恢復步驟                                                                     | 預估時間 |
| ------------------------ | ----------------------------- | ---------------------------------------------------------------------------- | -------- |
| **設定檔損毀**           | 啟動報錯、精靈無法讀取設定    | 1. 備份舊 `config.json` 2. 刪除或改名 3. 重新執行 `setup` 精靈              | 3 分鐘   |
| **Keyring 無法存取**     | OAuth 登入失敗、Token 讀取錯誤 | 1. `pocket-agent logout` 清除 2. 檢查 Keyring 服務 3. 重新 `setup` 登入     | 5 分鐘   |
| **會話檔案損毀/HMAC 失敗** | `/session` 無法載入、報簽章錯誤 | 1. 刪除損毀的 `sessions/*.json` 2. 重新開始對話 (歷史遺失)                  | 1 分鐘   |
| **Portable 版執行檔損壞** | `pocket.bat` 報 Python 錯誤   | 1. 刪除 `PocketAgent/` 目錄 2. 重新解壓縮 zip 或下載新版                    | 3 分鐘   |
| **沙盒目錄佔滿磁碟**     | 寫入失敗、Housekeeping 失敗   | 1. 手動清理 `~/.pocket-agent/workspaces/` 2. 調整 `housekeeping_max_size_mb` | 5 分鐘   |
| **防毒軟體隔離執行檔**   | `pythonw.exe` 被隔離、無法啟動 | 1. 加入防毒例外 2. 從隔離區還原 3. 或重新解壓縮 Portable 版                 | 10 分鐘  |

---

## 4. 疑難排解 (Troubleshooting)

### 4.1 常見問題速查表

| 症狀          | 可能原因                                    | 診斷指令/檢查                                    | 解決方案                                                    | 風險等級 |
| ------------- | ------------------------------------------- | ------------------------------------------------ | ----------------------------------------------------------- | -------- |
| **啟動即閃退** | Python 版本不符、依賴缺失、Portable 解壓不全 | `python --version`、查看 `agent.log` ERROR       | 確認 Python 3.14+、重新 `pip install -r requirements.txt`、重新解壓縮 | 高       |
| **OAuth 登入失敗** | 瀏覽器未開啟、Port 1455 被佔用、網路不通 | `netstat -an \| findstr 1455`、防火牆規則、瀏覽器彈窗攔截 | 手動開啟授權 URL、關閉佔用埠的程序、允許防火牆、關閉彈窗攔截 | 中       |
| **模型無回應/超時** | API 金鑰無效、額度耗盡、網路不穩、模型過載 | `/usage` 檢查額度、`agent.log` 連線錯誤、重試機制 | 更新 API Key、等待額度恢復、檢查網路、降低 `reasoning_effort` | 中       |
| **工具執行被攔截** | Default-Deny 政策、未在 safe_read_tools    | CLI 顯示 `[y/N]` 確認提示、`mcp_config.yaml` 白名單 | 加入 `safe_read_tools`、或輸入 `y` 確認、`A` 加入允許清單   | 低       |
| **檔案寫入失敗** | 權限不足、路徑不存在、Artifact 重導向       | `agent.log` WARNING/ERROR、檢查路徑、確認確認回呼 | 以管理員執行、確認路徑、輸入 `y`/`a`/`A` 允許              | 中       |
| **中文顯示亂碼** | 終端機編碼非 UTF-8、字體不支援              | `chcp 65001` (Windows)、終端機設定 UTF-8、安裝中文字體 | 設定終端機 UTF-8、使用支援中文的字體 (如 Cascadia Code)     | 低       |
| **MCP 連線失敗** | SSH/RDP/DB 參數錯誤、防火牆、服務未啟動     | `agent.log` `[ERROR]`、工具回傳錯誤訊息、手動測試連線 | 修正 `mcp_config.yaml` env、開放防火牆、啟動目標服務、重新輸入密碼 | 中       |
| **上下文超載** | `Context Overflow`、模型報 400 錯誤         | `/usage` 顯示 ratio > 90%、`agent.log` compaction 記錄 | 輸入 `/compact`、調大 `context_window`、減少附件/工具輸出   | 高       |
| **子代理卡住** | 無限迴圈、等待確認、網路逾時                | `agent.log` DivergenceGuard 警告、Ctrl+C 中斷   | Ctrl+C 強制中斷、檢查任務描述是否明確、調整 `max_tool_rounds` | 中       |

### 4.2 關鍵日誌位置

| 日誌類型     | 路徑/查詢方式                                                    | 保留期限                 | 關鍵字過濾                                   |
| ------------ | ---------------------------------------------------------------- | ------------------------ | -------------------------------------------- |
| **應用日誌** | `~/.pocket-agent/workspaces/<slug>/logs/agent_YYYY-MM-DD.log`    | 14 天 (Housekeeping)     | `ERROR`, `WARN`, `Context Overflow`, `mcp`   |
| **MCP Stdio** | `~/.pocket-agent/workspaces/<slug>/logs/mcp_stdio.log`          | 14 天                    | `ERROR`, `Connection closed`, `auth`         |
| **系統日誌** | Windows: 事件檢視器 / Linux: `journalctl -u <service>`          | 系統預設                 | `python`, `pocket-agent`                     |
| **審計日誌** | 會話 JSON 內含 HMAC、操作記錄                                     | 依 `/prune` (預設 30 天) | `hmac`, `signature`, `tool_call`             |

> **查看日誌實用指令**：
> ```cmd
> :: Windows CMD
> type ~/.pocket-agent/workspaces/*/logs/agent_*.log | findstr "ERROR"
> type ~/.pocket-agent/workspaces/*/logs/agent_*.log | findstr "mcp"
> 
> :: PowerShell (若可用)
> Get-Content ~/.pocket-agent/workspaces/*/logs/agent_*.log -Tail 100 | Select-String "ERROR"
> ```

### 4.3 關鍵指標與健康檢查

| 指標         | 正常範圍                    | 異常閾值                     | 檢查方式                                    |
| ------------ | --------------------------- | ---------------------------- | ------------------------------------------- |
| **啟動時間** | < 3 秒                      | > 10 秒                      | `Measure-Command { pocket-agent --version }` |
| **回應延遲** | 首字 < 5 秒 (視模型/網路)   | > 30 秒無回應                | 觀察 CLI Spinner、「思考中...」動畫          |
| **Token 用量** | 依模型/對話長度             | `/usage` 顯示 ratio > 90%    | 輸入 `/usage` 或 `/quota`                   |
| **會話檔案數** | < 100 筆                    | > 500 筆或 > 50 MB           | `dir ~/.pocket-agent\sessions`              |
| **沙盒目錄大小** | < 50 MB (單工作區)          | > `housekeeping_max_size_mb` | `du -sh ~/.pocket-agent/workspaces/*`       |
| **Housekeeping** | 每 24h 執行 1 次            | 連續 3 次失敗                | `grep "Housekeeping" agent.log`             |

---

## 5. 維護作業

### 5.1 例行維護排程

| 作業        | 頻率             | 執行窗口           | 指令/步驟                                      | 負責人            | 影響範圍           |
| ----------- | ---------------- | ------------------ | ---------------------------------------------- | ----------------- | ------------------ |
| **Housekeeping** | 每 24 小時     | Agent 啟動後、冷卻期過期 | 自動 (背景任務) / 手動 `/clean`               | 系統自動          | 僅清理沙盒暫存檔   |
| **Session Prune** | 依需求         | 使用者手動         | `/prune [30] [--keep 20]`                      | 使用者            | 刪除舊會話歷史     |
| **依賴更新** | 每月/發布前      | 維護窗口           | `pip-compile requirements.in` + `pip install` | 開發者            | 需重新啟動驗證     |
| **Portable 重建** | 重大版本/安全更新 | 發布流程           | `build.bat` → 推送 tag → GitHub Actions        | 發布工程師        | 使用者需重新下載   |
| **日誌歸檔** | 每季             | 維護窗口           | 複製 `~/.pocket-agent/workspaces/*/logs/` 至備份 | 使用者/管理員     | 無服務影響         |

### 5.2 版本升級檢查清單

- [ ] 變更日誌審閱 (Breaking Changes：Provider 介面、Tool Schema、Config 結構)
- [ ] 相依性安全掃描 (`pip-audit` / `safety check` / GitHub Dependabot)
- [ ] 設定檔相容性檢查 (`config.json` 新欄位、預設值變更)
- [ ] MCP 伺服器相容性 (`mcp_config.yaml` 結構、工具命名空間)
- [ ] 整合測試全數通過 (`pytest tests/`)
- [ ] 手動驗證：OAuth 登入、各 Provider 切換、工具呼叫、MCP 連線
- [ ] 效能基準測試 (啟動時間、首字延遲、記憶體用量)
- [ ] 回滾方案確認 (保留舊版 Portable zip、Git tag)
- [ ] 文件同步更新 (README、架構文件、CHANGELOG)
- [ ] 利害關係人通知 (Release Notes、Discord/Slack 發布)

---

## 6. 安全事件應變

### 6.1 通報流程

```mermaid
flowchart TD
    Detect[發現安全事件\n(日誌異常/使用者回報/自動偵測)] --> Assess[評估嚴重度\nP0/P1/P2]
    Assess -->|P0 立即| Contain[隔離措施\n停用受影響 MCP/工具\n撤銷 Token]
    Assess -->|P1 1小時內| Investigate[調查根因\n檢視日誌/審計/會話]
    Assess -->|P2 24小時內| Document[記錄發現\n更新防護規則]
    Contain --> Notify[通報團隊/使用者\n必要時向平台回報]
    Investigate --> Remediate[修復/緩解\n更新程式碼/設定/輪換金鑰]
    Document --> Remediate
    Remediate --> Verify[驗證修復\n測試/滲透測試/審計]
    Verify --> Close[結案\n發布公告/更新文件]
    Notify --> Close
```

### 6.2 應變等級定義

| 等級   | 定義                                                         | 響應時間        | 升級條件                    | 指揮官           |
| ------ | ------------------------------------------------------------ | --------------- | --------------------------- | ---------------- |
| **P0** | 主動攻擊進行中、機密外洩、RCE 漏洞被利用、大規模服務中斷     | 立即 (15 分鐘內) | 任何 P0 條件成立            | 專案負責人       |
| **P1** | 高風險漏洞 (公開 PoC)、單一使用者資料外洩、關鍵功能失效     | 1 小時內         | P0 未解決超過 1 小時、影響擴大 | 安全工程師       |
| **P2** | 中低風險漏洞、配置錯誤、非關鍵功能異常、資訊洩露風險        | 24 小時內        | P1 未解決超過 4 小時、可惡用 | 開發工程師       |

### 6.3 關鍵安全操作手冊

| 操作                  | 指令/步驟                                                                 | 適用場景                      |
| --------------------- | ------------------------------------------------------------------------- | ----------------------------- |
| **撤銷 OAuth Token**  | `pocket-agent logout` → 重新 `setup` 登入                                 | Token 洩露、帳號被盜用         |
| **停用 MCP Server**   | 編輯 `mcp_config.yaml` 設定 `enabled: false` → 重啟 Agent                 | MCP 供應鏈攻擊、工具行為異常   |
| **封鎖特定工具**      | 從 `allowed_tools` 移除、或 `mcp_config.yaml` 移出 `safe_read_tools`      | 特定工具被濫用、漏洞利用       |
| **輪換 API Key**      | 各平台 Console 撤銷舊 Key → 產生新 Key → `setup` 更新或 `config.json` 修改 | API Key 洩露、定期輪換         |
| **清除所有本機資料**  | 刪除 `~/.pocket-agent/` 目錄 → 重新 `setup`                               | 嚴重入侵、需完全重置環境       |
| **離線模式啟動**      | 拔網路線 / 防火牆封鎖 → 僅使用 Ollama 本地模型 + 本地工具                 | 網路被攻擊、需隔離環境繼續工作 |

---

## 7. 連絡資訊

| 角色           | 聯絡方式                    | 備註                          |
| -------------- | --------------------------- | ----------------------------- |
| **專案維護者** | GitHub Issues / Discussions | https://github.com/zanta0529/pocket-agent/issues |
| **安全回報**   | GitHub Security Advisories  | 私密回報：https://github.com/zanta0529/pocket-agent/security/advisories/new |
| **功能建議**   | GitHub Discussions          | https://github.com/zanta0529/pocket-agent/discussions |
| **文件問題**   | GitHub Issues (label: docs) | 標籤 `docs`、 `translation`   |

---

## 8. 運維流程圖

```mermaid
flowchart TD
    subgraph Daily["日常運維"]
        StartAgent[啟動 Agent] --> AutoHK[自動 Housekeeping]
        AutoHK --> UserWork[使用者正常操作]
        UserWork --> MonitorLog[監控 agent.log]
        MonitorLog -->|異常| Troubleshoot[疑難排解 4.1]
        Troubleshoot --> Resolve[解決/升級]
    end
    
    subgraph Weekly["週維護"]
        CheckSessions[檢查會話數量/大小] --> Prune[/prune 清理]
        CheckDisk[檢查沙磁碟用量] --> CleanHK[/clean 手動清理]
        CheckUpdates[檢查依賴/安全更新] --> UpdateDeps[更新依賴]
    end
    
    subgraph Monthly["月維護"]
        ReviewSecurity[審視安全日誌/審計] --> RotateKeys[輪換 API Keys]
        ReviewPerf[效能基準測試] --> TuneConfig[調整預算/逾時參數]
        BackupConfig[備份設定/會話] --> VerifyRestore[驗證恢復流程]
    end
    
    subgraph Incident["安全事件"]
        Detect[偵測/接報] --> Assess[評估等級 P0/P1/P2]
        Assess -->|P0| Immediate[立即隔離/撤銷/通報]
        Assess -->|P1| Urgent[1小時內調查/修復]
        Assess -->|P2| Scheduled[24小時內處理]
        Immediate --> PostMortem[事後根因分析]
        Urgent --> PostMortem
        Scheduled --> PostMortem
        PostMortem --> Improve[改善防護/更新文件]
    end
```

> **架構師風險評估**：
>
> - [x] 災難恢復演練頻率是否足夠：建議每季手動演練完整還原流程 (Portable 解壓縮 + 設定還原 + 會話載入)
> - [x] 備份恢復時間是否符合 RTO 要求：核心功能 < 5 分鐘、完整環境 < 10 分鐘，符合預期
> - [x] 單點故障是否已識別並緩解：Keyring 依賴 OS、JWE fallback、Portable 內建 Python 降低環境依賴
> - [x] 憑證輪換是否自動化：OAuth Access Token 自動 Refresh (60秒前)；API Key 需手動輪換
> - [x] 容量規劃是否有預警機制：Housekeeping `max_size_mb`、`max_files` 硬性上限；`/usage` 即時顯示 Context 用量
> - [!] 潛在風險：無集中式監控/告警系統 (本機工具特性)；依賴使用者主動檢查日誌；企業部署建議整合 SIEM 收集 `agent.log`

---

## 9. 變更記錄

| 版本        | 日期     | 作者       | 變更摘要 |
| ----------- | -------- | ---------- | -------- |
| 0.1.0       | 2025-01-15 | PocketAgent Team | 初版建立 (由 generate-system-docs 自動產生) |