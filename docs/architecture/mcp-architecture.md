# Pocket Agent MCP 伺服器掛載與注入機制

Model Context Protocol (MCP) 是標準化的 AI 工具橋接協定。Pocket Agent 實作了強健的 MCP Manager（`src/pocket_agent/extensions/mcp.py`），除了完整支援標準的 **Stdio**、**SSE** 與 **Streamable HTTP** 三大傳輸層，以及遠端 **MCP OAuth 2.1（PKCE + OS Keyring 儲存）** 與自訂 **HTTP Headers** 認證外，更加入了**機密金鑰注入**與**沙盒隔離**兩大企業級安全機制。

## 架構與安全攔截機制視覺化

```mermaid
flowchart TD
    Config[mcp_config.yaml] --> LoadMCP[載入所有 MCP Server]
    LoadMCP --> ReadSchema[讀取工具的 JSON Schema]

    ReadSchema --> StripInject{是否設定 inject 參數?}
    StripInject -- 是 --> RemoveSecret[將機密欄位從 Schema 中抹除]
    StripInject -- 否 --> CheckArtifact{是否為檔案寫入工具?}
    RemoveSecret --> CheckArtifact

    CheckArtifact -- 是 --> AddMeta[在 Schema 強制擴充 ArtifactMetadata]
    CheckArtifact -- 否 --> Register[向 LLM 註冊可用的 Tools]
    AddMeta --> Register

    Register --> LLMCall[LLM 決定呼叫工具]

    LLMCall --> CheckAuth{工具授權檢查}
    CheckAuth -- 具有 _safe_read 前綴或列於 safe_read_tools --> InjectArg{底層還原 Inject 與連線參數}
    CheckAuth -- 未知工具 (Default-Deny) --> RequireConfirm[要求使用者互動確認]
    RequireConfirm -- 同意 --> InjectArg
    RequireConfirm -- 拒絕 --> Block[攔截並回傳錯誤]

    InjectArg --> CheckConn{是否缺少連線參數 / 帳密?}
    CheckConn -- 是 (設定留空或未填) --> PromptConn[暫停 Spinner 於終端機互動詢問並快取於記憶體]
    CheckConn -- 否 (已設定或已快取) --> InterceptPath{攔截檔案路徑}
    PromptConn --> InterceptPath
    InterceptPath -- 若標記 UserFacing --> RedirectPath[強制將儲存路徑重導向至 ~/.pocket-agent/artifacts/]
    InterceptPath -- 其他情況 --> Exec[實際傳遞給 MCP Server 執行]
    RedirectPath --> Exec
```

## 核心機制解析

### 1. 自動注入與 Schema 抹除機制 (Security Inject)

當我們串接外部 MCP Server（例如資料庫查詢、API 操作）時，經常需要提供敏感的 API Key 或連線字串。
如果直接將這些欄位暴露在 Tool Schema 中，模型可能會「試圖」填寫，或者我們必須在系統提示詞中明碼寫出金鑰，這存在極大的資安風險。

Pocket Agent 的解決方案：

- 在 `mcp_config.yaml` 的 `inject` 區段設定金鑰。
- **註冊階段**：`MCPManager` 會自動攔截 Server 回報的 JSON Schema，將 `inject` 指定的參數**從 Schema 中徹底抹除**，因此 LLM 根本不知道有這個參數的存在。
- **執行階段**：當 LLM 呼叫該工具時，`MCPManager` 會在底層自動將 `inject` 參數合併回 Payload 中，再送交給 MCP Server 執行。實現「模型免接觸、伺服器能授權」的安全架構。

### 2. Artifact 全域隔離機制

為防止 AI 的自主行為污染使用者本地端的工作區程式碼，Pocket Agent 針對所有具備檔案寫入能力的工具（如 `write_to_file`, `write_file`, `create_file`）進行了強制攔截：

- **動態 Schema 擴充**：系統會在註冊階段強制要求這些工具必須帶有 `ArtifactMetadata` 參數，強制 LLM 交代寫入檔案的目的與是否面向使用者 (`UserFacing`)。
- **路徑重導向 (Redirect)**：在執行階段，若判斷為產出物，系統會無視 LLM 給予的路徑，強制將目標路徑替換到 `~/.pocket-agent/artifacts/<filename>`，確保沙盒外的專案原始碼安全無虞。

### 3. 工具授權與 `safe_read_tools` 白名單策略

為了防止惡意或未預期的 MCP 工具執行破壞性操作，Pocket Agent 實作了預設拒絕 (Default-Deny) 授權模型：

- **白名單自動放行**：工具名稱帶有特定前綴（如 `_safe_read`），或在 `mcp_config.yaml` 的伺服器設定中明確列於 `safe_read_tools` 清單內的唯讀安全工具（系統會在 Schema 標記 `"_is_safe_read": True`），在被 LLM 呼叫時會自動放行執行。
- **未知工具攔截**：對於所有未在白名單內或不具備安全標記的工具，系統預設視為高風險操作。當 LLM 試圖呼叫這些工具時，執行緒會被暫停，並在 CLI 介面強制要求使用者互動確認 (`[y/N]`)。若使用者拒絕，該次工具呼叫會被直接攔截並回傳錯誤給 LLM，保障系統安全。

### 4. 彈性連線參數配置與執行期互動詢問機制

針對需要遠端連線憑證的 MCP 工具（例如 `rhel_mcp.py` 的 `ssh_host`／`ssh_port`／`ssh_user`／`ssh_password` 對應 `RHEL_HOST`／`RHEL_SSH_PORT`／`RHEL_SSH_USER`／`RHEL_SSH_PASSWORD`，以及 `win_rdp_mcp.py` 的 `rdp_host`／`rdp_port`／`rdp_user`／`rdp_password` 對應 `WIN_HOST`／`WIN_RDP_PORT`／`WIN_RDP_USER`／`WIN_RDP_PASSWORD`），`MCPManager` 提供兼顧便利性與零落盤安全的混合解析機制：

1. **Schema 自動隱藏**：註冊工具時，`MCPManager` 會自動將該工具宣告的 `ssh_*` 或 `rdp_*` 連線參數從公開給 LLM 的 JSON Schema 中抹除，確保模型不會看到或嘗試猜測連線資訊與密碼。
2. **設定檔與環境變數優先**：執行工具時，依序檢查 `mcp_config.yaml` 的 `env` 設定、當次 Session 記憶體快取（`_runtime_conn_cache`）與系統環境變數。已設定且非空白的項目會直接使用。
3. **缺漏項目動態詢問（`on_prompt_conn`）**：若任何一項連線參數留空字串 `""` 或直接省略，`MCPManager` 會在首次呼叫工具前觸發前端回呼（CLI 會安全暫停 `rich.status` 動畫後透過 `Prompt.ask` 詢問，其中密碼強制使用 `password=True` 隱藏輸入；若無前端回呼則由 MCP Server 透過系統底層 TTY `CONIN$`／`/dev/tty` 詢問）。
4. **記憶體快取與失敗自動清除**：詢問取得的連線資訊僅暫存於當次執行期的記憶體中，同一對話後續呼叫無需重複輸入；一旦工具回傳 `[ERROR]`（例如認證失敗或連線逾時），系統會自動清除記憶體快取，以便下次重新提示輸入。所有敏感欄位在 `agent.log` 除錯日誌中一律遮罩為 `[REDACTED]`。

### 5. 遠端 Streamable HTTP 傳輸層與 MCP OAuth 2.1（OS Keyring 整合）

針對採用新世代 `Streamable HTTP` 傳輸協定或需要認證的外部遠端 MCP 伺服器（例如 `https://mcp.agensi.io/mcp/auth`），`MCPManager` 提供零外部 Node.js 依賴的原生整合：

1. **彈性傳輸協定識別**：`transport` 支援設定為 `Streamable HTTP`、`streamable-http`、`streamable_http` 或 `http`，底層直接透過 `mcp.client.streamable_http.streamable_http_client` 與 `truststore` 系統憑證庫建立連線。
2. **MCP OAuth 2.1 動態授權與零明碼落盤（`KeyringTokenStorage`）**：
   - 當遠端伺服器回應 HTTP `401 Unauthorized` 時，`OAuthClientProvider` 會自動執行 Protected Resource Metadata 探索與 Dynamic Client Registration，並開啟瀏覽器引導使用者完成 OAuth 2.1 PKCE 登入。
   - 授權取得的 `OAuthToken` 與 `OAuthClientInformationFull` 一律透過作業系統原生 `keyring`（`mcp-oauth-<server>` 與 `mcp-client-<server>`）分段加密儲存，絕不以明碼寫入磁碟；重新連線時自動載入並計算有效期限，到期時優先以 `refresh_token` 背景續期。
3. **自訂 HTTP Headers 與環境變數展開**：若遠端伺服器採用靜態金鑰或 Bearer Token，可在設定檔配置 `headers`（支援 `${ENV_VAR}` 語法從 `env` 或系統環境變數代入）或 `bearer_token`；當已明確指定 `Authorization` 標頭時，預設略過 OAuth 動態流程。
4. **Windows `stdio` 子行程 `stderr` 安全導向**：針對本機 `stdio` 伺服器，`MCPManager` 會將 `errlog` 導向工作區沙盒日誌 `mcp_stdio.log` 的實體檔案控制代碼，避免 Windows `CREATE_NO_WINDOW` 旗標導致 `.cmd`／Node.js 子行程因主控台 `stderr` 失效而發生 `Connection closed` 異常退出。

---

## 內建 RHEL 遠端巡檢與維運伺服器 (`mcp_servers/rhel_mcp.py`)

`rhel_mcp.py` 為內建的遠端 Red Hat Enterprise Linux (RHEL) 結構化唯讀巡檢與維運支援伺服器，支援原始碼開發模式、Portable 免安裝版與 PyInstaller 單一執行檔（`PocketAgent-cli.exe --internal-mcp-server`）跨目錄啟動。

### 1. 提供的結構化工具清單

所有工具均為唯讀操作，不會在遠端主機執行任何狀態修改，建議於 `mcp_config.yaml` 中加入 `safe_read_tools`：

| 工具名稱                   | 用途說明                                                                                                                                                                                             | 工具參數（供 LLM 呼叫）                                                                                                          |
| :------------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------- |
| `rhel_system_overview`     | 取得發行版資訊 (`/etc/os-release`)、Kernel（含已安裝版本 `rpm -q kernel --last` 與預設開機核心 `grubby --default-kernel`）、Uptime、時間同步 (`timedatectl`/`chronyc tracking`)、Tuned Profile、I/O 等待與 PSI 壓力指標 (`vmstat -w`／`/proc/pressure/*`)、記憶體與前 10 大高負載行程 | 無                                                                                                                               |
| `rhel_storage_and_fs`      | 檢查檔案系統空間 (`df -hT`)、Inodes (`df -i`)、區塊裝置 (`lsblk`) 與分割區及 `/dev/shm` 掛載安全選項 (`findmnt --list`)                                                                              | 無                                                                                                                               |
| `rhel_network_status`      | 檢查網路介面 (`ip -br addr`)、路由表 (`ip route`)、DNS (`/etc/resolv.conf`) 與監聽埠及對應行程 (`ss -tulnp`)                                                                                         | 無                                                                                                                               |
| `rhel_service_status`      | 查詢 systemd 服務狀態：未指定服務或 `failed_only=True` 時回傳前 50 筆結構化狀態（`UNIT LOAD ACTIVE SUB`，將 `@instance` 遮罩為 `@*`，不含 `DESCRIPTION`）與 `matching_units=<NR>`；指定時回傳結構化屬性（含 `UnitFileState`、`TriggeredBy`、`NRestarts` 與狀態時間戳記） | `service_name`（選填，支援單一服務或以逗號／空白分隔最多 10 個合法單元名稱 `[a-zA-Z0-9_.@-]+` 批次查詢）、`failed_only`（選填，預設 `False`） |
| `rhel_query_journal`       | 查詢 `journalctl` 系統或特定服務日誌之有界等級計數摘要（不回傳訊息原文）                                                                                                                             | `unit`（選填）、`priority`（預設 `err`）、`since`（預設 `1 hour ago`）、`lines`（1~200）                                         |
| `rhel_security_status`     | 檢查 SELinux 狀態 (`sestatus`/`getenforce`) 與 Firewalld 狀態（含預設區域 `--get-default-zone`、作用中區域 `--get-active-zones`、Runtime `--list-all` 與 Permanent `--permanent --list-all` 規則摘要） | 無                                                                                                                               |
| `rhel_read_config`         | 安全摘要模式僅回傳 `/etc/selinux/config` 的批准 `SELINUX` 列舉值（`enforcing`／`permissive`／`disabled`），不提供任意設定檔原文                                                                      | `file_path`（必填，僅允許 `/etc/selinux/config`）                                                                                |
| `rhel_selinux_context`     | 查詢白名單目錄下指定路徑的目前 SELinux 安全標籤 (`ls -dZ`) 與政策預期標籤 (`matchpathcon`)                                                                                                           | `target_path`（必填）                                                                                                            |
| `rhel_process_inspect`     | 深度探查行程資訊：指定 PID 檢視狀態摘要、以關鍵字搜尋行程（含無結果容錯保護），或依 CPU／Memory 排序（支援 SELinux 標籤）                                                                            | `pid`（選填）、`search`（選填）、`sort_by`（`cpu`/`mem`）、`show_selinux`、`max_lines`                                           |
| `rhel_network_connections` | 利用 `ss` 結構化探查網路連線狀態（支援 TCP/UDP 協定、連線狀態過濾、特定連接埠與行程關聯）                                                                                                            | `protocol`、`state`、`port`、`show_process`、`max_lines`                                                                         |
| `rhel_package_info`        | 查詢已安裝 RPM 套件資訊 (`rpm -qi`)、近期 Changelog 摘要與檔案完整性驗證 (`rpm -V --noscripts`)                                                                                                      | `package_name`（必填）、`verify_integrity`（預設 `False`）                                                                       |
| `rhel_cron_and_timers`     | 查詢已載入 systemd timer 排程清單（`systemctl list-timers --all`，含上次／下次執行時間與觸發目標服務）、已安裝 timer 啟用狀態（`list-unit-files --type=timer`，遮罩 `@instance` 為 `@*`）與 `/etc/crontab` 項目計數（不回傳 crontab 帳號或執行命令） | 無                                                                                                                               |
| `rhel_hardware_info`       | 探查硬體規格與虛擬化環境：虛擬化平台 (`systemd-detect-virt`)、CPU 架構 (`lscpu`) 與記憶體／Swap 明細                                                                                                 | 無                                                                                                                               |

### 2. 安全防禦邊界

- **雙軌制連線認證（無全域金鑰／Agent 後門）**：底層透過系統 OpenSSH `ssh`（搭配零明文落盤之 `SSH_ASKPASS_REQUIRE=force` 暫存腳本與子行程環境變數）支援「指定私鑰驗證（含 Passphrase）」與「加固帳密驗證」智慧切換，並強制附加 `-o GSSAPIAuthentication=no`、`-o IdentitiesOnly=yes`、`-o IdentityAgent=none`，徹底停用本機全域 `~/.ssh/` 金鑰、SSH Agent 與 Kerberos 驗證管道。
- **零任意 Shell 執行與參數防注入**：不提供通用 `run_command` 工具；所有遠端指令皆為固定模板（包覆於 `set -E -o pipefail` 診斷腳本），且外部參數皆經過嚴格正規表示式驗證與 `shlex.quote` 轉義。
- **路徑真實性驗證與安全摘要隔離**：透過 `realpath -e` 解析真實路徑並阻擋敏感關鍵字；`rhel_read_config` 僅回傳批准的 `SELINUX` 列舉值，`rhel_query_journal` 僅回傳等級計數，`rhel_service_status` 列舉清單時自動遮罩 `@instance` 參數並捨棄 `DESCRIPTION` 與命令列。
- **敏感資訊遮罩、串流防爆與集中稽核**：單次擷取硬性限制 `1 MB` 位元組上限（`MAX_CAPTURE_BYTES`）與 UTF-8 嚴格解碼，回傳結果自動將敏感鍵值與私鑰遮罩為 `[REDACTED]` 並限制輸出上限為 `12,000` 字元；所有遠端執行與安全攔截事件均自動寫入集中化 `agent.log` 的 `[AUDIT] [MCP:rhel-ops]` 紀錄。

### 3. 環境變數與配置參數表 (`servers.rhel_ops.env`)

| 參數名稱                  | 必填 / 選填 | 預設值         | 空白或省略時的行為                                             | 說明                                                                                     |
| :------------------------ | :---------- | :------------- | :------------------------------------------------------------- | :--------------------------------------------------------------------------------------- |
| `RHEL_HOST`               | 彈性        | `""`           | 首次呼叫工具時於終端機互動詢問目標主機 IP 或 Hostname          | 目標 RHEL 主機的 IP 位址（支援 IPv4／IPv6）或網域名稱。                                  |
| `RHEL_SSH_PORT`           | 彈性        | `"22"`         | 若省略或留空，首次呼叫工具時於終端機互動詢問（預設帶入 `22`）   | 目標 RHEL 主機的 SSH 連接埠（`1` ~ `65535`）。                                           |
| `RHEL_SSH_USER`           | 彈性        | `""`           | 首次呼叫工具時於終端機互動詢問登入帳號                         | 登入遠端主機的 SSH 使用者帳號。                                                          |
| `RHEL_SSH_PASSWORD`       | 彈性        | `""`           | 未指定私鑰時，首次呼叫工具於終端機以隱藏輸入方式詢問登入密碼   | 登入遠端主機的 SSH 密碼（或私鑰 Passphrase）。建議留空，於執行期動態輸入以確保不落盤。   |
| `RHEL_SSH_KEY_PATH`       | 選填        | `""`           | 留空時採用加固帳密驗證；有指定時切換為指定私鑰驗證模式         | 指定用於認證的 SSH 私鑰檔案路徑（搭配 `IdentitiesOnly=yes`）。                           |
| `RHEL_SSH_KEY_PASSPHRASE` | 選填        | `""`           | 金鑰模式若設有環境變數則自動經由 `SSH_ASKPASS` 解鎖加密私鑰    | 加密 SSH 私鑰的通行碼（Passphrase）。                                                    |
| `RHEL_CONNECT_TIMEOUT`    | 選填        | `"10"`         | 套用預設值 `10` 秒（範圍 `3` ~ `60` 秒）                       | SSH 連線逾時秒數。                                                                       |
| `RHEL_STRICT_HOST_KEY`    | 選填        | `"accept-new"` | 套用預設值 `"accept-new"`（搭配專屬 `rhel_known_hosts` 檔案）  | 設為 `"yes"`／`"strict"`／`"true"`／`"1"` 時強制啟用嚴格主機金鑰檢查（`StrictHostKeyChecking=yes`）。 |

---

## 內建 Windows 遠端桌面視覺與唯讀巡檢伺服器 (`mcp_servers/win_rdp_mcp.py`)

針對僅開放標準遠端桌面協定（RDP, Port 3389）且未啟用 WinRM／RPC／OpenSSH 的目標 Windows 主機，`win_rdp_mcp.py` 採用企業 RPA 標準的**雙層混合架構（Dual-Layer RDP Bridge）**，結合 **Layer A（GUI 畫面截圖與鍵鼠模擬）** 與 **Layer B（經由 RDP 剪貼簿虛擬通道 `CLIPRDR` 執行 `| clip` 無損文字巡檢）**。

### 1. 提供的工具清單

| 分層    | 工具名稱                  | 建議列入 `safe_read_tools` | 用途說明                                                                                      | 工具參數（供 LLM 呼叫）                                                        |
| :------ | :------------------------ | :------------------------- | :-------------------------------------------------------------------------------------------- | :----------------------------------------------------------------------------- |
| Layer A | `win_rdp_screenshot`      | 是                         | 擷取 `mstsc.exe` 遠端桌面視窗畫面並儲存為 PNG 圖檔，回傳路徑與視窗客戶區尺寸                  | `monitor_wait_sec`（選填，預設 `0.5` 秒）                                      |
| Layer A | `win_rdp_mouse_action`    | **否（強制互動確認）**     | 於遠端桌面視窗相對座標 `(x, y)` 執行滑鼠點擊、雙擊、右鍵、移動或滾輪                          | `action`、`x`、`y`、`button`（預設 `left`）、`scroll_delta`                    |
| Layer A | `win_rdp_keyboard_action` | **否（強制互動確認）**     | 於遠端桌面視窗輸入文字（`type_text`）或發送按鍵／組合鍵（`key_press`／`hotkey`）              | `action`、`text`（選填）、`keys`（選填，如 `enter`、`win+r`）                  |
| Layer A | `win_rdp_disconnect`      | 是                         | 關閉目前由 MCP 管理的 `mstsc.exe` 遠端桌面視窗並重置連線狀態                                  | 無                                                                             |
| Layer B | `win_system_overview`     | 是                         | 透過 RDP 剪貼簿通道取得 `hostname`、`systeminfo` 與 `wmic cpu` 系統概況                       | 無                                                                             |
| Layer B | `win_storage_and_fs`      | 是                         | 透過 RDP 剪貼簿通道取得邏輯磁碟機與磁碟區容量、剩餘空間及檔案系統                             | 無                                                                             |
| Layer B | `win_network_status`      | 是                         | 透過 RDP 剪貼簿通道取得 `ipconfig /all`、IPv4 路由表與 `netstat -ano` 監聽埠                  | 無                                                                             |
| Layer B | `win_service_status`      | 是                         | 查詢指定 Windows Service 的執行狀態 (`sc.exe queryex`) 與啟動設定 (`sc.exe qc`)               | `service_name`（必填，嚴格限制為 `[a-zA-Z0-9_.\-]+`）                          |
| Layer B | `win_query_eventlog`      | 是                         | 透過 `wevtutil.exe qe` 查詢 Windows 事件檢視器日誌                                            | `log_name`（`System`/`Application`/`Security`/`Setup`）、`level`、`lines`      |
| Layer B | `win_security_status`     | 是                         | 檢查 Windows 防火牆狀態 (`netsh advfirewall`)、當前權限 (`whoami /priv`) 與近期失敗登入 (4625) | 無                                                                             |
| Layer B | `win_read_config`         | 是                         | 唯讀檢視白名單目錄下的 Windows 設定檔內容（最多 200 行，自動遮罩機密欄位）                    | `file_path`（必填，如 `C:\Windows\System32\drivers\etc\hosts`）、`max_lines` |

### 2. 安全防禦邊界

- **強制純帳號密碼驗證與零憑證殘留**：動態產生的 `.rdp` 設定檔強制設定 `redirectsmartcards:i:0`、`redirectwebauthn:i:0`、`redirectdrives:i:0` 與 `prompt for credentials:i:0`，徹底關閉智慧卡、Windows Hello 與本機磁碟掛載後門。啟動 `mstsc.exe` 時透過 Win32 `CredWriteW` 寫入僅存活於當次登入記憶體（`CRED_PERSIST_SESSION`）的暫存憑證，並於視窗握手完成後（`finally` 區塊）**立即呼叫 `CredDeleteW` 將憑證刪除**，確保 `cmdkey /list` 不殘留任何免密登入憑證。
- **視窗座標沙盒與前景焦點保護**：`win_rdp_mouse_action` 嚴格驗證 `(x, y)` 必須位於 `mstsc.exe` 客戶區範圍內，拒絕點擊視窗外之本機桌面；`win_rdp_keyboard_action` 強制驗證前景視窗（`GetForegroundWindow`）確為遠端桌面視窗後才發送按鍵，並封鎖 `Ctrl+Alt+Del`、`Win+L`、`Alt+Tab` 等系統熱鍵。
- **設定檔白名單與機密遮罩**：`win_read_config` 僅允許讀取 `C:\Windows\System32\drivers\etc\`、`C:\Windows\win.ini`、`C:\Windows\system.ini`、`C:\inetpub\wwwroot\`、`C:\ProgramData\`，並攔截包含 `SAM`、`SECURITY`、`ntds.dit`、`web.config`、`.pfx`、`.key`、`.env` 等敏感路徑，回傳文字亦自動遮罩密碼與 ConnectionString。

### 3. 環境變數與配置參數表 (`servers.win_rdp.env`)

| 參數名稱             | 必填 / 選填 | 預設值       | 空白或省略時的行為                                             | 說明                                                                                   |
| :------------------- | :---------- | :----------- | :------------------------------------------------------------- | :------------------------------------------------------------------------------------- |
| `WIN_HOST`           | 彈性        | `""`         | 首次呼叫工具時於終端機互動詢問目標 Windows 主機 IP 或 Hostname | 目標 Windows 主機的 IP 位址或網域名稱。                                                |
| `WIN_RDP_PORT`       | 彈性        | `"3389"`     | 若省略或留空，首次呼叫工具時於終端機互動詢問（預設帶入 `3389`） | 目標 Windows 主機的 RDP 遠端桌面連接埠（`1` ~ `65535`）。                              |
| `WIN_RDP_USER`       | 彈性        | `""`         | 首次呼叫工具時於終端機互動詢問登入帳號                         | 登入遠端 Windows 的帳號（支援 `User`、`Domain\User` 或 `User@Domain`）。               |
| `WIN_RDP_PASSWORD`   | 彈性        | `""`         | 首次呼叫工具時於終端機以隱藏輸入（不回顯）方式詢問登入密碼     | 登入遠端 Windows 的密碼。建議留空或省略，於對話執行期動態輸入以確保設定檔不落盤明文密碼。 |
| `WIN_RDP_TIMEOUT`    | 選填        | `"15"`       | 套用預設值 `15` 秒                                             | 等待 RDP 視窗連線與剪貼簿通道回傳結果的逾時秒數（`5` ~ `60`）。                        |
| `WIN_RDP_RESOLUTION` | 選填        | `"1280x800"` | 套用預設視窗解析度 `1280x800`                                  | 遠端桌面視窗客戶區解析度（格式 `<寬>x<高>`，範圍 `640x480` ~ `3840x2160`）。           |

---

## 內建 SQLite 本機資料庫唯讀檢索伺服器 (`mcp_servers/sqlite_mcp.py`)

`sqlite_mcp.py` 為隨附的本機 SQLite 資料庫結構探查與唯讀查詢伺服器。透過 SQLite Authorizer 與唯讀 URI 強制執行沙盒防護，杜絕寫入或破壞性異動。

### 1. 提供的結構化工具清單

所有工具均為唯讀操作，建議於 `mcp_config.yaml` 中加入 `safe_read_tools`：

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 工具參數（供 LLM 呼叫） |
| :--- | :--- | :--- | :--- |
| `sqlite_list_tables` | 是 | 列出資料庫中的所有非系統資料表與檢視表（`VIEW`）清單 | 無 |
| `sqlite_describe_schema` | 是 | 顯示指定資料表或檢視表的 Schema（欄位、型別、主鍵、`NOT NULL`、`DEFAULT` 預設值、索引與外鍵） | `table_name`（必填） |
| `sqlite_run_select_query` | 是 | 執行唯讀的 `SELECT`、`WITH` 或 `EXPLAIN` 查詢（具備串流防爆與機密欄位遮罩） | `query`（必填，僅限 `SELECT`／`WITH`／`EXPLAIN SELECT` 語句） |
| `sqlite_switch_database` | 是（路徑動態授權） | 於對話中切換目前連線的 SQLite 資料庫檔案；以唯讀連線驗證為合法 SQLite 後才生效，失敗時保留原連線 | `target_db_path`（必填） |

> 註：為維持向下相容性，亦提供 `db_list_tables`、`db_describe_schema`、`db_run_select_query` 與 `db_switch_database` 之別名支援。

#### 切換資料庫的兩種方式

1. **自然語言**：直接請 AI「切換資料庫到 `data/sales.db`」，模型會呼叫 `sqlite_switch_database`。
2. **CLI 斜線指令**：輸入 `/mcp sqlite <資料庫路徑>` 直接切換（不經模型推論、零 Token）；輸入 `/mcp sqlite` 可檢視目前生效的資料庫路徑。

切換成功後，`MCPManager` 會將新路徑寫入當次執行期快取 `_runtime_conn_cache`，後續所有 `sqlite_*` 查詢工具自動注入新路徑（不寫回 `mcp_config.yaml`，重新啟動後恢復設定檔預設值）。

### 2. 安全防禦邊界

- **URI mode=ro 與 Authorizer 雙重唯讀防護**：連線以 `file:<path>?mode=ro` URI 開啟，並於連線底層掛載 `sqlite3.set_authorizer`，嚴格封鎖所有 INSERT、UPDATE、DELETE、DROP、ALTER 等異動指令，並封鎖未授權的 PRAGMA 操作（僅允許 `table_info`、`index_list`、`index_info`、`foreign_key_list` 等結構探查 PRAGMA）。
- **SQL 語意前綴與單一語句檢查**：清除開頭註解後僅允許以 `SELECT`、`WITH`（CTE）或 `EXPLAIN SELECT` 開頭，嚴格阻絕多重語句（`;`）注入或寫入關鍵字。
- **串流防爆與 OOM 防護**：採用 `fetchmany` 批次提取，單次輸出嚴格受 `MAX_OUTPUT_CHARS`（預設 12,000 字元）限制，超限時自動友善截斷並提示加上 `WHERE` 或 `LIMIT/OFFSET`。
- **欄位與內容雙重機密遮罩**：查詢結果自動針對敏感欄位名稱（`_SENSITIVE_COL_RE`，如 `password_hash`、`api_token`、`secret`）整欄遮罩為 `[REDACTED]`，並針對一般欄位的敏感鍵值、連線 URI 內嵌帳密（`scheme://user:pass@host`）與 PEM 私鑰進行正規遮罩。
- **連線保證釋放與彈性路徑快取**：查詢後立即在 `finally` 區塊關閉連線，杜絕 Windows 平台檔案佔用鎖死問題；`DB_PATH` 支援由 `MCPManager` 隱藏參數並在未設定時暫停 CLI 動畫互動詢問（記憶體快取具 10 分鐘閒置過期與失敗自動清除機制）。
- **UNC 網路路徑封鎖**：任何檔案系統操作前，先拒絕以 `\\` 或 `//` 開頭的 UNC／網路路徑，避免 Windows 對外發起 SMB 連線而洩漏 NetNTLMv2 認證雜湊。
- **切換工具的工作區動態授權**：唯讀不等於無風險。若 `sqlite_switch_database` 列入 `safe_read_tools` 卻不限制路徑，遭提示詞注入的模型可在無確認下切換至瀏覽器 `Cookies`、`Login Data` 等工作區外 SQLite 檔案並查詢。因此 `Agent` 會以 `_validate_safe_path` 檢查 `target_db_path`：位於工作區內（CWD、`/add-dir` 授權目錄）才免確認，並正規化為絕對路徑；工作區外或命中敏感黑名單（`.git`、`.env*`、`.pocket-agent` 設定）時仍彈出 `[y/N]` 確認，無確認機制時直接拒絕。`/mcp sqlite <路徑>` 亦比照 `/file` 指令，工作區外需確認。

### 3. 環境變數與配置參數表 (`servers.sqlite.env`)

| 參數名稱 | 必填 / 選填 | 預設值 | 說明 |
| :--- | :--- | :--- | :--- |
| `DB_PATH` | 彈性 | `""` | 目標 SQLite 資料庫檔案的絕對或相對路徑（例如 `dev.db`）；留空或省略時於首次呼叫工具時互動詢問並快取於記憶體。 |

---

## 內建遠端關聯式資料庫唯讀檢索伺服器 (`mcp_servers/mysql_mcp.py` 與 `mcp_servers/mssql_mcp.py`)

`mysql_mcp.py` 與 `mssql_mcp.py` 分別支援遠端 MySQL／MariaDB 與 Microsoft SQL Server 之結構探查與唯讀查詢。採用延遲載入（`importlib.import_module`）機制，MySQL 優先使用 `pymysql`（次選 `mysql-connector-python`），SQL Server 優先使用 `pymssql`（次選 `pyodbc`）。

### 1. 提供的結構化工具清單

| 伺服器 | 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 工具參數（供 LLM 呼叫） |
| :--- | :--- | :--- | :--- | :--- |
| `mysql` | `mysql_list_databases` | 是 | 列出遠端 MySQL／MariaDB 主機上目前帳號可見的所有資料庫清單 | 無 |
| `mysql` | `mysql_list_tables` | 是 | 列出目標資料庫（或所有非系統資料庫）中的所有基底資料表與檢視表（`VIEW`） | `database`（選填） |
| `mysql` | `mysql_describe_schema` | 是 | 透過參數化查詢 `INFORMATION_SCHEMA` 顯示欄位、型別、主鍵、`NOT NULL`、`DEFAULT` 預設值、索引與外鍵 | `table_name`（必填）、`database`（選填） |
| `mysql` | `mysql_run_select_query` | 是 | 執行唯讀的單一 `SELECT`、`WITH` 或 `EXPLAIN` 查詢（具備串流防爆與機密欄位遮罩） | `query`（必填） |
| `mssql` | `mssql_list_databases` | 是 | 列出遠端 SQL Server 主機上所有線上（ONLINE）資料庫清單 | 無 |
| `mssql` | `mssql_list_tables` | 是 | 列出目標 SQL Server 資料庫中的所有基底資料表與檢視表（`VIEW`） | `database`（選填） |
| `mssql` | `mssql_describe_schema` | 是 | 透過參數化查詢顯示欄位、完整型別（含長度與精度）、主鍵、`NOT NULL`、`DEFAULT` 預設值、索引與外鍵 | `table_name`（必填，支援 `table` 或 `schema.table`）、`database`（選填） |
| `mssql` | `mssql_run_select_query` | 是 | 執行唯讀的單一 `SELECT` 或 `WITH` 查詢（具備串流防爆與機密欄位遮罩） | `query`（必填） |

### 2. 安全防禦邊界

- **連線與交易層級強制唯讀**：`mysql_mcp.py` 連線後立即執行 `SET SESSION TRANSACTION READ ONLY` 與 `START TRANSACTION READ ONLY`；`mssql_mcp.py` 啟用 `ApplicationIntent=ReadOnly` 與 `SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED`，且兩者皆設為 `autocommit=False`，並於 `finally` 區塊強制 `rollback()` 與 `close()`。
- **單一查詢與危險關鍵字封鎖**：清除開頭註解（含 `--`、`/* */` 及 MySQL `#`）後僅允許 `SELECT` 或 `WITH`（MySQL 亦支援 `EXPLAIN`）開頭，嚴格禁止多重語句（分號注入）以及 `INTO`、`FOR UPDATE`、`EXEC`、`EXECUTE`、`xp_cmdshell`、`OPENROWSET`、`BULK` 等具寫入或系統副作用之語法。
- **彈性憑證配置與機密遮罩**：`MYSQL_HOST`／`MYSQL_PORT`／`MYSQL_USER`／`MYSQL_PASSWORD`／`MYSQL_DATABASE` 與 `MSSQL_HOST`／`MSSQL_PORT`／`MSSQL_USER`／`MSSQL_PASSWORD`／`MSSQL_DATABASE` 皆支援設定檔預填（含 IPv6 位址正規化比對）、留空或省略並於執行期互動詢問（快取於記憶體，認證失敗自動清除）；查詢結果自動針對敏感欄位名稱、機密鍵值、連線 URI 內嵌帳密與 PEM 私鑰遮罩為 `[REDACTED]`。

---

## 內建 GitHub PR／Issue 整合伺服器 (`mcp_servers/github_mcp.py`)

`github_mcp.py` 提供 GitHub REST API 之 Pull Request 列表檢索、Issue／PR 詳情讀取與留言新增功能，並整合企業環境 TLS 憑證鏈與雙向機密防護。

### 1. 提供的結構化工具清單

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 工具參數（供 LLM 呼叫） |
| :--- | :--- | :--- | :--- |
| `github_list_prs` | 是 | 列出指定儲存庫的 Pull Requests（支援狀態篩選與分頁） | `owner`、`repo`、`state`（預設 `"open"`，可設 `"closed"`／`"all"`）、`per_page`（`1`~`100`）、`page`（預設 `1`） |
| `github_read_issue` | 是 | 讀取指定 Issue 或 Pull Request 的標題、作者、狀態與內文 | `owner`、`repo`、`issue_number`（正整數） |
| `github_create_comment` | **否（強制互動確認）** | 在指定的 Issue 或 Pull Request 新增留言 | `owner`、`repo`、`issue_number`、`body` |

### 2. 安全防禦邊界

- **嚴格路徑驗證與儲存庫白名單**：強制驗證 `owner` 與 `repo` 符合 `^[A-Za-z0-9_.-]+$`（禁止 `.`、`..`、`/`、`?`、`#`），並對 URL 路徑段落執行百分比編碼，徹底防範 `#` 片段截斷與跨端點注入；支援透過 `GITHUB_ALLOWED_REPOS` 限定允許存取的 `owner/repo` 清單。
- **彈性憑證解析與企業 TLS 支援**：`GITHUB_TOKEN` 支援透過 `MCPManager` 隱藏參數（`github_token`）於執行期動態詢問並快取於記憶體（閒置 10 分鐘或遭遇 HTTP `401` 自動清除）；獨立子行程自動優先注入 `truststore` 系統憑證庫以相容企業 SSL 代理。
- **雙向機密防護、輸出截斷與集中稽核**：讀取結果自動遮罩私鑰、Token 與連線帳密並受 `MAX_OUTPUT_CHARS`（預設 12,000 字元）限制；`github_create_comment` 於送出前自動掃描 `body`，若偵測到私鑰或真實 Token 立即阻斷外發，並將所有操作與攔截事件寫入 `agent.log` 的 `[AUDIT] [MCP:github]` 紀錄。

### 3. 環境變數與配置參數表 (`servers.github.env`)

| 參數名稱 | 必填 / 選填 | 預設值 | 說明 |
| :--- | :--- | :--- | :--- |
| `GITHUB_TOKEN` | 彈性 | `""` | GitHub Personal Access Token；建議留空或省略，於首次呼叫工具時在 CLI 以隱藏輸入方式動態提供並快取於記憶體。 |
| `GITHUB_ALLOWED_REPOS` | 選填 | `""` | 允許存取之儲存庫白名單（逗號分隔 `owner/repo`，不分大小寫）；留空時允許存取該 Token 授權範圍內之儲存庫。 |
