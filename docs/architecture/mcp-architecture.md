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

### 0. 啟動開關與防呆機制 (`enabled`)

在 `mcp_config.yaml` 的各伺服器設定區塊中可選填 `enabled: true/false`（省略時預設為 `true`）：

- 設為 `false` 時，`MCPManager.start()` 會在連線階段前略過該伺服器，不啟動本機子行程或建立遠端連線，也不會將其工具註冊至 LLM Context，同時保留完整的設定內容。
- 若顯式設定為非布林值（例如字串 `"false"`、`0` 或 `null`），`is_mcp_server_enabled()` 會寫入警告日誌並預設視為停用，防止因 YAML 型別誤寫而意外啟用伺服器。
- 啟動完成後，CLI 會依 `MCPStartupResult.disabled_servers` 顯示已略過的停用伺服器數量，`setup` 精靈亦會在設定清單中標記停用狀態。

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

### 6. 隨附 MCP 伺服器模組化分層架構（`mcp_servers/`）

所有隨附之 Stdio MCP 伺服器採用「**共用基礎設施（`_shared/`）＋ 領域子套件（`<domain>/`）＋ 頂層薄入口（`<name>_mcp.py`）**」三層設計：

- **`mcp_servers/_shared/`**：集中維護 `bootstrap`（路徑初始化、系統常數對齊 `CONSTANTS` 與純鍵值降級 `t()`）、`redaction`、`audit`、`tty`、`validation`、`credentials`、`registry`（Factory `create_mcp_server`）與 `db_base`（`SqlGuard` Strategy 與資料庫共用基底）。
- **領域子套件（`browser/`、`sqlite/`、`mysql/`、`mssql/`、`github/`、`rhel/`、`win_rdp/`、`netdiag/`）**：內部按安全守門／連線轉接（Adapter）／工具群組（`tools/*.py` 的 `register(mcp)` Command-Registrar）／組裝器（`server.py`）切分職責。
- **頂層薄入口（`*_mcp.py`）與輕量單檔（`memory_mcp.py`）**：維持既有 `mcp_config.yaml` 與 `--internal-mcp-server`（`runpy.run_path`）之穩定啟動契約。

---

## 內建 RHEL 遠端巡檢與維運伺服器 (`mcp_servers/rhel_mcp.py`)

`rhel_mcp.py` 為內建的遠端 Red Hat Enterprise Linux (RHEL) 結構化唯讀巡檢與維運支援伺服器，支援原始碼開發模式、Portable 免安裝版與 PyInstaller 單一執行檔（`PocketAgent-cli.exe --internal-mcp-server`）跨目錄啟動。

### 1. 提供的結構化工具清單

所有工具均為唯讀操作，不會在遠端主機執行任何狀態修改，建議於 `mcp_config.yaml` 中加入 `safe_read_tools`：

| 工具名稱 | 用途說明 | 工具參數（供 LLM 呼叫） |
| :------------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------- |
| `rhel_system_overview` | 取得發行版資訊 (`/etc/os-release`)、Kernel（含已安裝版本 `rpm -q kernel --last` 與預設開機核心 `grubby --default-kernel`）、Uptime、時間同步 (`timedatectl`/`chronyc tracking`)、Tuned Profile、I/O 等待與 PSI 壓力指標 (`vmstat -w`／`/proc/pressure/*`)、記憶體與前 10 大高負載行程 | 無 |
| `rhel_health_snapshot` | 單次 SSH 往返綜合基線健檢：彙整 OS／Kernel、記憶體／PSI、磁碟／Inode、失敗服務、SELinux／Firewalld 狀態與近 1 小時 `err` 以上日誌計數（無原文） | 無 |
| `rhel_kernel_health` | 探查核心健康狀態：`/proc/sys/kernel/tainted` 位元解碼、`oom_kill` 次數、`/proc/sys/fs/file-nr` 開啟水位、`threads-max` 與 `journalctl -k` 警告等級計數（無原文） | `since`（預設 `24 hours ago`） |
| `rhel_storage_and_fs` | 檢查檔案系統空間 (`df -hT`)、Inodes (`df -i`)、區塊裝置 (`lsblk -e 7` 含 `TYPE`/`ROTA`)、LVM VG／LV（含 Thin Pool 使用率）、`/proc/mdstat` 與掛載安全選項 (`findmnt --list`) | 無 |
| `rhel_network_status` | 檢查網路介面 (`ip -br addr`)、介面封包錯誤統計 (`ip -s -br link`)、IPv4／IPv6 路由表 (`ip route`／`ip -6 route`)、DNS (`/etc/resolv.conf`) 與監聽埠及對應行程 (`ss -tulnp`) | 無 |
| `rhel_service_status` | 查詢 systemd 服務狀態：未指定服務或 `failed_only=True` 時回傳前 50 筆結構化狀態（`UNIT LOAD ACTIVE SUB`，將 `@instance` 遮罩為 `@*`，不含 `DESCRIPTION`）與 `matching_units=<NR>`；指定時回傳結構化屬性（含 `UnitFileState`、`TriggeredBy`、`NRestarts` 與狀態時間戳記） | `service_name`（選填，支援單一服務或以逗號／空白分隔最多 10 個合法單元名稱 `[a-zA-Z0-9_.@-]+` 批次查詢）、`failed_only`（選填，預設 `False`） |
| `rhel_query_journal` | 查詢 `journalctl` 系統或特定服務日誌之有界等級計數摘要（含 `truncated` 旗標，不回傳訊息原文） | `unit`（選填）、`priority`（預設 `err`）、`since`（預設 `1 hour ago`）、`lines`（1~200） |
| `rhel_security_status` | 檢查 SELinux 狀態 (`sestatus`/`getenforce`) 與 Firewalld 狀態（含預設區域 `--get-default-zone`、作用中區域 `--get-active-zones`、Runtime `--list-all` 與 Permanent `--permanent --list-all` 規則摘要） | 無 |
| `rhel_read_config` | 透過白名單結構化策略摘要系統設定檔（支援 `/etc/selinux/config`、`/etc/ssh/sshd_config`、`/etc/sysctl.conf`、`/etc/security/limits.conf` 與遮罩敏感掛載選項之 `/etc/fstab`），絕不回傳任意檔案原文 | `file_path`（必填，限上述 5 個白名單路徑）、`max_lines`（1~120，預設 `120`） |
| `rhel_selinux_context` | 經遠端 `realpath -e` 白名單守衛驗證後，查詢指定檔案或目錄的目前 SELinux 安全標籤 (`ls -dZ`) 與政策預期標籤 (`matchpathcon`) | `target_path`（必填） |
| `rhel_cert_expiry` | 巡檢白名單公開憑證目錄（如 `/etc/pki/tls/certs`）下之 X.509 憑證到期日（僅輸出 `subject`／`issuer`／`enddate`，嚴格排除私鑰與敏感路徑） | `cert_dir`（預設 `/etc/pki/tls/certs`） |
| `rhel_process_inspect` | 深度探查行程資訊：指定 PID 檢視狀態摘要、以固定字串大小寫不敏感搜尋行程（保留表頭與無結果容錯），或依 CPU／Memory 排序（支援 SELinux 標籤） | `pid`（選填）、`search`（選填）、`sort_by`（`cpu`/`mem`）、`show_selinux`、`max_lines` |
| `rhel_network_connections` | 利用 `ss` 結構化探查網路連線狀態（支援 `summary_only` 執行 `ss -s` 統計摘要、TCP/UDP 協定、連線狀態過濾、特定連接埠與行程關聯） | `protocol`、`state`、`port`、`show_process`、`max_lines`、`summary_only`（預設 `False`） |
| `rhel_package_info` | 唯讀查詢本機 RPM 資料庫：單一套件資訊 (`rpm -qi`) 與完整性驗證 (`rpm -V --noscripts`)、近期安裝／更新套件 (`recent_count`) 或已安裝套件關鍵字搜尋 (`search`)（零 DNF 副作用） | `package_name`、`verify_integrity`（預設 `False`）、`recent_count`、`search` |
| `rhel_container_status` | 安全唯讀探查容器環境：systemd 服務與單元狀態，以及僅在既有 `docker.service`／`podman.socket` 已啟動時查詢最小容器欄位（`ID`／`Image`／`State`／`Status`／`Ports`） | 無 |
| `rhel_cron_and_timers` | 查詢已載入 systemd timer 排程清單（`systemctl list-timers --all`，含上次／下次執行時間與觸發目標服務）、已安裝 timer 啟用狀態（`list-unit-files --type=timer`，遮罩 `@instance` 為 `@*`）與 `/etc/crontab` 項目計數（缺檔自動歸零，不回傳 crontab 帳號或執行命令） | 無 |
| `rhel_hardware_info` | 探查硬體規格與虛擬化環境：虛擬化平台 (`systemd-detect-virt`)、CPU 架構 (`lscpu`) 與記憶體／Swap 明細 | 無 |

### 2. 安全防禦邊界

- **雙軌制連線認證（無全域金鑰／Agent 後門）**：底層透過系統 OpenSSH `ssh`（搭配零明文落盤之 `SSH_ASKPASS_REQUIRE=force` 暫存腳本與子行程環境變數）支援「指定私鑰驗證（含自動偵測加密私鑰並互動詢問 Passphrase）」與「加固帳密驗證」智慧切換，並強制附加 `-o GSSAPIAuthentication=no`、`-o IdentitiesOnly=yes`、`-o IdentityAgent=none`，徹底停用本機全域 `~/.ssh/` 金鑰、SSH Agent 與 Kerberos 驗證管道。
- **零任意 Shell 執行與參數防注入**：不提供通用 `run_command` 工具；所有遠端指令皆為固定模板（包覆於 `set -E -o pipefail` 診斷腳本），且外部參數皆經過嚴格正規表示式驗證與 `shlex.quote` 轉義。
- **路徑真實性驗證與安全摘要隔離**：透過 `realpath -e` 解析真實路徑並阻擋敏感關鍵字（含 `rhel_read_config`、`rhel_selinux_context` 與 `rhel_cert_expiry`）；`rhel_read_config` 僅透過白名單結構化解析策略回傳受控欄位，`rhel_query_journal` 僅回傳等級計數，`rhel_service_status` 列舉清單時自動遮罩 `@instance` 參數並捨棄 `DESCRIPTION` 與命令列。
- **敏感資訊遮罩、串流防爆與集中稽核**：單次擷取硬性限制 `1 MB` 位元組上限（`MAX_CAPTURE_BYTES`）與 UTF-8 嚴格解碼，回傳結果透過 `RhelRedactionPolicy` 自動將敏感鍵值與私鑰遮罩為 `[REDACTED]`、針對不安全多行內容採 fail-closed 拒收，並以 `safe_cut_index` 保護遮罩標記不被 `12,000` 字元截斷；所有遠端執行與安全攔截事件均自動寫入集中化 `agent.log` 的 `[AUDIT] [MCP:rhel-ops]` 紀錄。

### 3. 環境變數與配置參數表 (`servers.rhel_ops.env`)

| 參數名稱                  | 必填 / 選填 | 預設值         | 空白或省略時的行為                                             | 說明                                                                                     |
| :------------------------ | :---------- | :------------- | :------------------------------------------------------------- | :--------------------------------------------------------------------------------------- |
| `RHEL_HOST`               | 彈性        | `""`           | 首次呼叫工具時於終端機互動詢問目標主機 IP 或 Hostname          | 目標 RHEL 主機的 IP 位址（支援 IPv4／IPv6）或網域名稱。                                  |
| `RHEL_SSH_PORT`           | 彈性        | `"22"`         | 若省略或留空，首次呼叫工具時於終端機互動詢問（預設帶入 `22`）   | 目標 RHEL 主機的 SSH 連接埠（`1` ~ `65535`）。                                           |
| `RHEL_SSH_USER`           | 彈性        | `""`           | 首次呼叫工具時於終端機互動詢問登入帳號                         | 登入遠端主機的 SSH 使用者帳號。                                                          |
| `RHEL_SSH_PASSWORD`       | 彈性        | `""`           | 未指定私鑰時，首次呼叫工具於終端機以隱藏輸入方式詢問登入密碼   | 登入遠端主機的 SSH 密碼（或私鑰 Passphrase）。建議留空，於執行期動態輸入以確保不落盤。   |
| `RHEL_SSH_KEY_PATH`       | 選填        | `""`           | 留空時採用加固帳密驗證；有指定時切換為指定私鑰驗證模式         | 指定用於認證的 SSH 私鑰檔案路徑（搭配 `IdentitiesOnly=yes`）。                           |
| `RHEL_SSH_KEY_PASSPHRASE` | 選填        | `""`           | 金鑰模式若設有環境變數則自動經由 `SSH_ASKPASS` 解鎖；留空且私鑰加密時自動互動詢問 | 加密 SSH 私鑰的通行碼（Passphrase）。                                                    |
| `RHEL_CONNECT_TIMEOUT`    | 選填        | `"10"`         | 套用預設值 `10` 秒（範圍 `3` ~ `60` 秒）                       | SSH 連線逾時秒數。                                                                       |
| `RHEL_CMD_TIMEOUT`        | 選填        | `""`           | 留空時自動套用 `RHEL_CONNECT_TIMEOUT + 15` 秒（範圍 `5` ~ `120` 秒） | 遠端診斷指令執行逾時秒數。                                                               |
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
- **可信程序／視窗綁定與截圖失焦中止**：嚴格綁定本工具啟動之系統可信 `mstsc.exe` PID、映像路徑與 `TscShellContainerClass` 視窗類別；`win_rdp_screenshot` 強制使用 `ImageGrab.grab(window=hwnd)` 並於視窗失焦時立即中止。
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
| `sqlite_inspect_health` | 是 | 執行唯讀健康診斷（檔案大小、分頁／碎片率統計與受 500 MB 熔斷保護之 `PRAGMA quick_check`） | `run_quick_check`（選填，預設 `False`） |
| `sqlite_run_select_query` | 是 | 執行唯讀的 `SELECT`、`WITH` 或 `EXPLAIN` 查詢（具備串流防爆與機密欄位遮罩） | `query`（必填，僅限 `SELECT`／`WITH`／`EXPLAIN SELECT` 語句） |
| `sqlite_switch_database` | 是（路徑動態授權） | 於對話中切換目前連線的 SQLite 資料庫檔案；以唯讀連線驗證為合法 SQLite 後才生效，失敗時保留原連線 | `target_db_path`（必填） |

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
| `mysql` | `mysql_list_databases` | 是 | 列出遠端 MySQL／MariaDB 主機上目前帳號可見的所有資料庫清單（12,000 字元安全截斷） | 無 |
| `mysql` | `mysql_list_tables` | 是 | 列出目標資料庫（或所有非系統資料庫）中的所有基底資料表與檢視表（`VIEW`） | `database`（選填） |
| `mysql` | `mysql_describe_schema` | 是 | 透過參數化查詢 `INFORMATION_SCHEMA` 顯示欄位、型別、主鍵、`NOT NULL`、`DEFAULT`（遮罩）、`EXTRA`、欄位註解、`INDEX_TYPE` 索引與跨庫外鍵（跨庫同名表自動分庫呈現） | `table_name`（必填）、`database`（選填） |
| `mysql` | `mysql_show_create_table` | 是 | 唯讀取得指定資料表或檢視表的完整 `SHOW CREATE TABLE` DDL 定義（經遮罩與截斷） | `table_name`（必填）、`database`（選填） |
| `mysql` | `mysql_inspect_health` | 是 | 透過 `INFORMATION_SCHEMA` 分批限縮零鎖定巡檢碎片率、無主鍵資料表、`AUTO_INCREMENT` 水位與 `BTREE` 冗餘前綴索引 | `database`（選填）、`table_name`（選填） |
| `mysql` | `mysql_check_table` | **否（涉及索引頁讀取鎖）** | 針對單一資料表執行 `CHECK TABLE ... QUICK / FAST` 完整性檢查（具 2 GB 容量熔斷） | `table_name`（必填）、`database`（選填）、`check_mode`（`QUICK`/`FAST`） |
| `mysql` | `mysql_run_select_query` | 是 | 執行唯讀的單一 `SELECT`、`WITH` 或 `EXPLAIN` 查詢（具備串流防爆與機密欄位遮罩） | `query`（必填） |
| `mysql` | `mysql_explain_query` | 是 | 分析純 `SELECT`／`WITH` 查詢執行計畫（`TRADITIONAL`／`JSON`／`TREE`，禁止 `ANALYZE`），並自動標記全表掃描與 `filesort` 警訊 | `query`（必填）、`explain_format`（選填） |
| `mysql` | `mysql_server_status` | 是 | 查詢 MySQL／MariaDB 版本、Uptime、`read_only` 狀態、連線數水位與 InnoDB Buffer Pool 命中率（限白名單變數） | 無 |
| `mysql` | `mysql_inspect_processlist` | **否（含執行中 SQL 片段）** | 查詢 `INFORMATION_SCHEMA.PROCESSLIST` 長時間查詢與鎖等待（限前 50 筆，`INFO` 遮罩並限長 200 字元） | `only_active`（預設 `True`）、`min_time_sec`（預設 `0`） |
| `mysql` | `mysql_replication_status` | 是 | 查詢 `SHOW REPLICA STATUS`／`SHOW SLAVE STATUS`（僅採白名單回傳 IO／SQL 執行緒、延遲與 GTID，嚴格排除帳密欄位） | 無 |
| `mssql` | `mssql_list_databases` | 是 | 列出遠端 SQL Server 主機上所有線上（ONLINE）資料庫清單（12,000 字元安全截斷） | 無 |
| `mssql` | `mssql_list_tables` | 是 | 列出目標 SQL Server 資料庫中的所有基底資料表與檢視表（`VIEW`） | `database`（選填） |
| `mssql` | `mssql_describe_schema` | 是 | 透過參數化查詢顯示欄位、完整型別精度、主鍵、`DEFAULT`（遮罩）、`IDENTITY`／`COMPUTED`、`MS_Description`、`INCLUDE`／`WHERE` 索引與跨 Schema 外鍵 | `table_name`（必填，支援 `table`、`schema.table` 或 `db.schema.table`）、`database`（選填） |
| `mssql` | `mssql_get_object_definition` | 是 | 取得 View／Stored Procedure／Function／Trigger 的 T-SQL 定義（`sys.sql_modules`）或資料表完整結構摘要（經遮罩與截斷） | `object_name`（必填）、`database`（選填） |
| `mssql` | `mssql_inspect_health` | 是 | 透過零鎖定 DMV 分批限縮檢視空間、Heap 表、缺少主鍵、停用索引、未受信任約束、Identity 水位與非篩選 B-Tree 冗餘索引（無權限自動降級） | `database`（選填）、`table_name`（選填） |
| `mssql` | `mssql_inspect_missing_indexes` | 是 | 查詢 `sys.dm_db_missing_index_*` DMV，依預估影響分數排序列出缺索引建議 | `database`（選填）、`top_n`（`1`~`50`，預設 `20`） |
| `mssql` | `mssql_run_select_query` | 是 | 執行唯讀的單一 `SELECT` 或 `WITH` 查詢（具備串流防爆、截斷即取消與機密欄位遮罩） | `query`（必填） |
| `mssql` | `mssql_explain_query` | 是 | 透過 `SET SHOWPLAN_TEXT ON` 取得純編譯期預估計畫（絕不實際執行查詢），並標記 `Table Scan`、`Key Lookup` 與 `Sort` 警訊 | `query`（必填） |
| `mssql` | `mssql_server_status` | 是 | 查詢 `@@VERSION`、`SERVERPROPERTY`、HADR 狀態、啟動時間、CPU／記憶體與工作階段數（無 DMV 權限自動降級） | 無 |
| `mssql` | `mssql_inspect_active_requests` | **否（含執行中 SQL 片段）** | 查詢活動中 SQL Server 請求、等待類型與阻塞鏈（`blocking_session_id`，`TOP 50`，SQL 遮罩並限長 200 字元） | `min_elapsed_ms`（預設 `0`）、`blocking_only`（預設 `False`） |
| `mssql` | `mssql_backup_and_ag_status` | 是 | 檢查線上資料庫最近 `FULL`／`DIFF`／`LOG` 備份（逾 7 天或無完整備份警示）及 Always On AG 同步／佇列狀態 | `database`（選填） |

### 2. 安全防禦邊界

- **連線與交易層級強制唯讀、查詢逾時與 TLS**：`mysql_mcp.py` 連線後立即執行 `SET SESSION TRANSACTION READ ONLY`、`SET SESSION MAX_EXECUTION_TIME`（MariaDB 降級 `max_statement_time`）與 `START TRANSACTION READ ONLY`，並支援 `MYSQL_SSL_MODE`／`MYSQL_SSL_CA` TLS 加密連線；`mssql_mcp.py` 啟用 `ApplicationIntent=ReadOnly`、`pyodbc conn.timeout`、`SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED` 與 `SET LOCK_TIMEOUT`，且兩者皆設為 `autocommit=False`，於 `finally` 區塊強制 `rollback()` 與 `close()`（截斷大查詢時改以 `cursor.cancel()`／強制關閉連線避免排空）。
- **單一查詢、字面值感知註解正規化與串流熔斷**：清除開頭註解（含 `--`、`/* */` 及 MySQL `#`）後僅允許 `SELECT` 或 `WITH`（MySQL 亦支援 `EXPLAIN`）開頭，嚴格禁止多重語句（分號注入）以及 `INTO`、`FOR UPDATE`／`FOR SHARE`、`LOAD_FILE()`、`WAIT_FOR_EXECUTED_GTID_SET()`、`UPDLOCK`／`TABLOCKX`、`DM_OS_ENUMERATE_FILESYSTEM`、`FN_GET_AUDIT_FILE`、`EXEC`、`EXECUTE`、`xp_cmdshell`、`OPENROWSET`、`BULK` 等具寫入、鎖定或系統副作用之語法。`mysql_mcp.py` 與 `mssql_mcp.py` 皆採用引號狀態機之註解正規化與字面值遮蔽骨架（同時避免誤殺字串內的 `;` 或關鍵字），`mysql_mcp.py` 採 `SSCursor`／`buffered=False` 串流游標；`*_inspect_health` 採零鎖定詮釋資料／DMV／受限 `PRAGMA` 巡檢（分批限縮於前 500 張表並排除 Filtered Index 誤報），`mysql_check_table` 限定單表 `QUICK`／`FAST` 模式與 `2 GB` 容量熔斷，`mysql_replication_status` 與 `mssql_backup_and_ag_status` 嚴格以白名單排除帳號／主機／憑證欄位。
- **彈性憑證配置與機密遮罩**：`MYSQL_HOST`／`MYSQL_PORT`／`MYSQL_USER`／`MYSQL_PASSWORD`／`MYSQL_DATABASE` 與 `MSSQL_HOST`／`MSSQL_PORT`／`MSSQL_USER`／`MSSQL_PASSWORD`／`MSSQL_DATABASE` 皆支援設定檔預填（含 IPv6 位址正規化比對）、留空或省略並於執行期互動詢問（快取於記憶體，認證失敗自動清除）；查詢結果自動針對敏感欄位名稱、機密鍵值、連線 URI 內嵌帳密與 PEM 私鑰遮罩為 `[REDACTED]`。

---

## 內建 GitHub PR／Issue 整合伺服器 (`mcp_servers/github_mcp.py`)

`github_mcp.py` 提供 GitHub REST API 之 Pull Request／Issue 檢索與更新、行內 Code Review 讀取與提交、CI Check／Actions Workflow 狀態與 Annotations 診斷、儲存庫唯讀探索、三階段範本感知 PR 建立及留言功能，並整合連線池重用、企業環境 TLS 憑證鏈、GitHub Enterprise Server（GHES）端點支援與雙向機密防護。

### 1. 提供的結構化工具清單

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 工具參數（供 LLM 呼叫） |
| :--- | :--- | :--- | :--- |
| `github_list_prs` | 是 | 列出指定儲存庫的 Pull Requests（含 Draft 與 `head -> base` 分支資訊） | `owner`、`repo`、`state`（預設 `"open"`）、`head`、`base`、`sort`、`direction`、`per_page`（`1`~`100`）、`page`（預設 `1`） |
| `github_get_pr` | 是 | 取得單一 Pull Request 完整詳情（含 `head.sha`、分支方向、合併狀態、待審查者與變更統計） | `owner`、`repo`、`pull_number`（正整數） |
| `github_get_pr_files` | 是 | 取得指定 PR 的變更檔案清單、行數統計與各檔 Diff Patch | `owner`、`repo`、`pull_number`、`per_page`、`page`、`include_patch`（預設 `True`） |
| `github_get_pr_reviews` | 是 | 取得指定 PR 的 Code Review 審查紀錄（`APPROVED`、`CHANGES_REQUESTED` 等）與審查意見 | `owner`、`repo`、`pull_number`、`per_page`、`page` |
| `github_get_pr_review_comments` | 是 | 讀取指定 PR 掛載於程式碼檔案與行號上的行內審查留言（含 `diff_hunk` 與回覆串） | `owner`、`repo`、`pull_number`、`per_page`、`page` |
| `github_prepare_pr_context` | 是 | 依「本機範本 ➔ 遠端範本（含大小寫與 `docs/` 路徑） ➔ 內建業界標準範本」取得 PR 範本，並比對 `base...head` 分支變更摘要 | `owner`、`repo`、`head`（支援 `branch` 或 `owner:branch`）、`base`（選填，留空自動查詢 `default_branch`） |
| `github_get_pr_checks` | 是 | 查詢指定 Commit SHA、分支或標籤的 GitHub Actions／CI Check Runs 狀態（含 `check_run_id` 與摘要） | `owner`、`repo`、`ref`、`per_page`、`page` |
| `github_get_check_annotations` | 是 | 取得指定 Check Run 的詳細錯誤／警告註記（檔案、行號與訊息） | `owner`、`repo`、`check_run_id`（正整數）、`per_page`、`page` |
| `github_list_workflow_runs` | 是 | 列出指定儲存庫的 GitHub Actions Workflow 執行紀錄 | `owner`、`repo`、`branch`、`status`、`event`、`per_page`、`page` |
| `github_get_workflow_run_jobs` | 是 | 查詢指定 Workflow Run 內的 Jobs 與 Steps 執行狀態 | `owner`、`repo`、`run_id`（正整數）、`filter_mode`（`latest`/`all`）、`per_page`、`page` |
| `github_read_issue` | 是 | 讀取指定 Issue 或 Pull Request 的標題、作者、狀態、標籤、指派人、里程碑、留言數與內文 | `owner`、`repo`、`issue_number`（正整數） |
| `github_read_comments` | 是 | 讀取指定 Issue 或 Pull Request 的留言討論串（支援分頁） | `owner`、`repo`、`issue_number`、`per_page`、`page` |
| `github_list_issues` | 是 | 列出指定儲存庫的 Issues（支援狀態、標籤、指派人、建立者、里程碑與排序篩選，預設排除 PR） | `owner`、`repo`、`state`、`labels`、`assignee`、`creator`、`milestone`、`sort`、`direction`、`per_page`、`page`、`include_prs`（預設 `False`） |
| `github_search_issues` | 是 | 在指定儲存庫內以關鍵字搜尋 Issues 與 PR（強制鎖定 `repo:owner/repo` 範圍） | `owner`、`repo`、`query`、`state`（預設 `"all"`）、`per_page`、`page` |
| `github_get_repo` | 是 | 查詢指定儲存庫的預設分支、可見性、主要語言、Topics 與基本統計 | `owner`、`repo` |
| `github_list_branches` | 是 | 列出指定儲存庫的遠端分支清單（可篩選僅受保護分支） | `owner`、`repo`、`protected_only`（預設 `False`）、`per_page`、`page` |
| `github_list_tags` | 是 | 列出指定儲存庫的 Git Tags 清單 | `owner`、`repo`、`per_page`、`page` |
| `github_list_commits` | 是 | 列出指定儲存庫的 Commit 歷史紀錄（支援分支／SHA、路徑與作者篩選） | `owner`、`repo`、`sha`、`path`、`author`、`per_page`、`page` |
| `github_get_file_content` | 是 | 唯讀取得指定儲存庫與 `ref` 下的單一檔案內容或目錄清單（含二進位偵測與大小限制） | `owner`、`repo`、`path`（選填）、`ref`（選填） |
| `github_list_releases` | 是 | 列出指定儲存庫的 Releases 發行版本清單與摘要 | `owner`、`repo`、`per_page`、`page` |
| `github_list_labels` | 是 | 列出指定儲存庫目前可用的標籤（Labels）與說明 | `owner`、`repo`、`per_page`、`page` |
| `github_create_pr` | **否（強制預覽確認）** | 建立 Pull Request（CLI 強制彈出完整動作、分支方向、標題與內文預覽，可選附加標籤與審查者，且絕不自動 Merge） | `owner`、`repo`、`title`、`head`、`base`（選填）、`body`、`draft`（預設 `False`）、`labels`、`reviewers` |
| `github_update_pr` | **否（強制預覽確認）** | 更新既有 PR 的標題、內文、狀態（`open`/`closed`）或目標分支（`base`） | `owner`、`repo`、`pull_number`、`title`、`body`、`state`、`base` |
| `github_create_pr_review` | **否（強制預覽確認）** | 提交 PR 審查意見與行內留言（僅允許 `COMMENT` 或 `REQUEST_CHANGES`，**禁止 `APPROVE`**） | `owner`、`repo`、`pull_number`、`event`（預設 `"COMMENT"`）、`body`、`comments`、`commit_id` |
| `github_create_issue` | **否（強制互動確認）** | 在指定儲存庫建立新的 Issue（自動掃描標題、內文、標籤與指派人以防機密外洩） | `owner`、`repo`、`title`、`body`、`labels`、`assignees`、`milestone` |
| `github_update_issue` | **否（強制互動確認）** | 更新既有 Issue 的標題、內文、狀態、關閉原因、標籤、指派人或里程碑 | `owner`、`repo`、`issue_number`、`title`、`body`、`state`、`state_reason`、`labels`、`assignees`、`milestone` |
| `github_create_comment` | **否（強制互動確認）** | 在指定的 Issue 或 Pull Request 新增留言 | `owner`、`repo`、`issue_number`、`body` |

### 2. 安全防禦邊界

- **嚴格路徑驗證、搜尋範圍鎖定與重導向白名單**：強制驗證 `owner`、`repo`、`ref` 與儲存庫內相對 `path` 格式並執行 URL 路徑分段百分比編碼，徹底防範 `#` 片段截斷與 `..` 路徑穿越；`github_search_issues` 阻斷 `query` 內夾帶 `repo:`／`org:`／`user:` 範圍覆寫；支援 `GITHUB_ALLOWED_REPOS` 儲存庫白名單與選填 `GITHUB_API_BASE_URL`（強制 HTTPS 且驗證主機、連接埠與 API 前綴），並透過 `httpx` 事件鉤子攔截 HTTP `3xx` 重導向，阻斷跨網域、非 HTTPS 或未授權儲存庫之重導向繞過。
- **PR 建立／更新／審查強制預覽確認與人工核准合併把關（No Auto-Merge / No Auto-Approve）**：`github_create_pr`、`github_update_pr` 與 `github_create_pr_review` 由 `ConfirmationBridge` 強制攔截（不適用自動放行），於終端機面板完整列出預計執行的動作與內容預覽（`github_create_issue`、`github_update_issue` 與 `github_create_comment` 亦提供結構化預覽面板）；`github_create_pr_review` 強制阻擋 `APPROVE` 事件，且伺服器故意不提供任何 Merge 或刪除工具，確保最終核准與合併永遠由人工於 GitHub 網站把關。
- **彈性憑證解析、Keyring／JWE 持久化、非互動防死鎖與連線池重用**：`GITHUB_TOKEN` 支援透過 `MCPManager` 隱藏參數（`github_token`）於執行期動態詢問並快取於記憶體（閒置 10 分鐘或遭遇 HTTP `401` 自動清除）；使用者輸入後可選擇保存至作業系統原生 `keyring`（不支援時自動降級至主密碼 JWE 加密庫），待該次 API 呼叫未回傳 `401` 後即落盤並將 `mcp_config.yaml` 更新為 `"USE_KEYRING"`／`"USE_ENCRYPTED"` 佔位符號（保留註解與排版）。當使用者清空該佔位符號後，下次執行（含同會話熱偵測）將重新提示輸入；若選擇不保存則一併清除 Keyring 舊憑證。子行程在非互動 Stdio 管道下快速失敗拋出 `ConnAuthError` 交由主行程詢問，防止終端機 Spinner 競態死鎖；單一 Event Loop 下共用 `httpx.AsyncClient` TCP/TLS 連線池並單例快取 `truststore` 系統憑證庫注入。
- **雙向機密防護、預裁切防 ReDoS 與集中稽核**：單次回應受 `MAX_RESPONSE_BYTES`（2 MB）與 `MAX_OUTPUT_CHARS`（預設 12,000 字元）雙重限制，超長文字先以安全邊界緩衝預裁切再執行機密遮罩（保留邊界完整 `[REDACTED...]` 標記）；所有外發寫入工具（`github_create_pr`、`github_update_pr`、`github_create_pr_review`、`github_create_issue`、`github_update_issue`、`github_create_comment`）於送出前自動掃描私鑰、各雲端／AI 平台 Token、內嵌帳密 URL 與敏感鍵值對，偵測到時立即阻斷外發，並將所有操作寫入防日誌注入清洗之 `agent.log` `[AUDIT] [MCP:github]` 紀錄。

### 3. 環境變數與配置參數表 (`servers.github.env`)

| 參數名稱 | 必填 / 選填 | 預設值 | 說明 |
| :--- | :--- | :--- | :--- |
| `GITHUB_TOKEN` | 彈性 | `""` | GitHub Personal Access Token；建議留空（或設為 `"USE_KEYRING"`／`"USE_ENCRYPTED"`），留空時於首次呼叫工具於 CLI 以隱藏輸入提供並可選擇保存至系統 Keyring／JWE 加密庫；清空佔位符號將於下次執行重新詢問。 |
| `GITHUB_ALLOWED_REPOS` | 選填 | `""` | 允許存取之儲存庫白名單（逗號分隔 `owner/repo`，不分大小寫）；留空時允許存取該 Token 授權範圍內之儲存庫。 |
| `GITHUB_API_BASE_URL` | 選填 | `""` | 企業自架 GitHub Enterprise Server (GHES) 之 API 基底網址（須為 HTTPS 且不可含查詢參數或使用者憑證，例如 `https://github.corp.com/api/v3`）；留空時使用官方 `https://api.github.com`。 |

---

## 內建網站與網路跨單位舉證診斷伺服器 (`mcp_servers/netdiag_mcp.py`)

`netdiag_mcp.py` 專為權責分離環境下的網站負責人設計，提供唯讀、因果連貫的階段式網路舉證報告（`DNS → TCP → TLS 憑證鏈 → HTTP`）與多解析器 DNS 比對，預設停用（`enabled: false`）且**不建議列入 `safe_read_tools`**。

### 1. 提供的結構化工具清單

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 工具參數（供 LLM 呼叫） |
| :--- | :--- | :--- | :--- |
| `netdiag_diagnose` | **否（強制逐次確認）** | 單一分階段有界交易診斷，標註 `first_failure_stage`、雙軌併發 CA 對照（`truststore` vs `certifi`）、過期／臨期憑證、安全標頭／邊緣指紋、`http->https` 轉址 TLS 檢核、多採樣優先呈現失敗明細，並支援 Markdown／JSON 輸出 | `target`、`max_stage`（`dns`/`tcp`/`tls`/`http`）、`method`（`HEAD`/`GET`）、`host_header`、`ip_family`（`auto`/`ipv4`/`ipv6`）、`samples`（`1`~`3`）、`timeout_sec`（`3.0`~`15.0`）、`output_format`（`markdown`/`json`） |
| `netdiag_dns_compare` | **否（強制逐次確認）** | 基於 `dnspython` 比對系統預設 DNS 與指定解析器之紀錄差異、TTL、CNAME 鏈、`_dmarc.<domain>`、SPF 與 CAA 訊號（缺套件時降級 `socket`），支援 Markdown／JSON 輸出 | `domain`、`record_types`、`nameservers`（最多 4 個 IP）、`timeout_sec`、`output_format`（`markdown`/`json`） |

### 2. 安全防禦邊界

- **授權先於 DNS 與永久硬封鎖**：在任何 DNS 解析前先行驗證協定、URL 內嵌帳密、連接埠（預設 `80,443,8080,8443`）與 `NETDIAG_ALLOWED_DOMAINS`（區分精確主機與 `*.domain` 子網域規則）；永久封鎖 Link-Local／雲端 Metadata（`169.254.0.0/16`、`fe80::/10`、`fd00:ec2::254`）、Multicast 與未指定位址。
- **公私網分層治理與防 DNS Rebinding**：預設封鎖 RFC 1918／ULA 私有 IP 與 `localhost`；開啟 `NETDIAG_ALLOW_PRIVATE_NETWORKS=true` 後允許所有 RFC 1918／ULA（若設定 `NETDIAG_ALLOWED_CIDRS` 則進一步限縮）。採 Pinned IP 連線並於每跳 HTTP 轉址重審授權與網段（含 `http -> https` 升級轉址之 TLS 憑證檢核）。
- **企業 Proxy Fail-Closed 與 TLS 不降級**：遵循環境 Proxy 設定（`NO_PROXY` 支援 CIDR 與帶埠規則）並在代理失敗時立即中止（絕不靜默降級直連）；TLS 驗證失敗時中止 HTTP 階段，僅以零應用層資料之純 TLS 握手擷取公開憑證欄位供舉證。
- **CLI 結構化確認與防永久白名單**：`ConfirmationBridge` 渲染目標網段、埠口、SNI／Host 與階段預覽，強制逐次確認且禁止 `a`／`A` 寫入會話或永久白名單。

### 3. 環境變數與配置參數表 (`servers.netdiag.env`)

| 參數名稱 | 必填 / 選填 | 預設值 | 說明 |
| :--- | :--- | :--- | :--- |
| `NETDIAG_ALLOW_PRIVATE_NETWORKS` | 選填 | `"false"` | 設為 `"true"` 允許診斷內網 RFC 1918／ULA 私有 IP。 |
| `NETDIAG_ALLOWED_CIDRS` | 選填 | `""` | 啟用內網時選填之管轄 CIDR 白名單（留空則允許所有 RFC 1918／ULA）。 |
| `NETDIAG_ALLOWED_DOMAINS` | 選填 | `""` | 允許診斷之網域白名單（逗號分隔，支援 `*.example.org`）。 |
| `NETDIAG_ALLOWED_PORTS` | 選填 | `"80,443,8080,8443"` | 允許探測之連接埠白名單。 |
| `NETDIAG_ALLOW_LOOPBACK` | 選填 | `"false"` | 設為 `"true"` 才允許診斷 `localhost`／`127.0.0.1`／`::1`。 |
| `NETDIAG_ALLOW_PROXY` | 選填 | `"false"` | 預設 fail-closed 拒用環境 Proxy；企業代理環境請顯式設為 `"true"`（代理側 DNS／IP 須由代理 ACL 把關）。 |
| `NETDIAG_TIMEOUT` | 選填 | `"10"` | 單階段逾時秒數（範圍 `3` ~ `15` 秒）。 |

---

## 內建瀏覽器自動化伺服器 (`mcp_servers/browser_mcp.py`)

`browser_mcp.py` 對應 `mcp_servers/browser/` 領域子套件，透過本機既有的 Microsoft Edge／Google Chrome（或既有 CDP 除錯實例）提供受安全邊界約束的瀏覽器自動化與人工協作能力，且不強依賴 `playwright install` 下載瀏覽器二進位檔。

### 1. 提供的結構化工具清單

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 工具參數（供 LLM 呼叫） |
| :--- | :--- | :--- | :--- |
| `browser_navigate` | **否** | 正規化並驗證網址後開啟指定頁面（限 `http`/`https`） | `url`（必填） |
| `browser_back` | **否** | 返回瀏覽器上一頁並驗證頁面網址 | 無 |
| `browser_wait` | 是 | 等待頁面出現指定文字、網址片段或達到載入狀態（`load`/`domcontentloaded`/`networkidle`） | `text`、`url_contains`、`state`、`timeout_sec`（`1`~`30`） |
| `browser_snapshot` | 是 | 清除舊標記並為可見互動元素編上 `[ref]` 編號（支援 CSS 區域聚焦、敏感欄位遮罩與狀態標記） | `selector`（選填） |
| `browser_get_text` | 是 | 擷取頁面可見文字（經機密遮罩與安全邊界截斷至 12,000 字元） | 無 |
| `browser_screenshot` | 是 | 擷取頁面截圖並儲存於 `~/.pocket-agent/artifacts/browser_shots/`（零工作區污染） | `full_page`（預設 `False`） |
| `browser_click` | **否** | 點擊指定 `[ref]` 元素，並偵測是否開啟新分頁 | `ref`（必填正整數） |
| `browser_fill` | **否** | 清空並填入指定 `[ref]` 輸入框（密碼與敏感欄位強制遮罩回顯與稽核） | `ref`（必填）、`text`（必填） |
| `browser_select_option` | **否** | 於 `<select>` 下拉選單依 `value` 或 `label` 選取項目 | `ref`（必填）、`value`、`label` |
| `browser_press` | **否** | 發送鍵盤按鍵（如 `Enter`、`Tab`、`Escape`） | `key`（必填） |
| `browser_scroll` | **否** | 向上或向下捲動頁面（像素受 `100`~`3000` 有界限制） | `direction`（`up`/`down`）、`amount`（預設 `600`） |
| `browser_wait_for_user` | **否** | 遇 CAPTCHA／登入／兩步驟驗證時暫停，跨平台提示使用者手動完成（Win32／macOS／TTY） | `message`（選填） |
| `browser_list_tabs` | 是 | 列出目前所有開啟分頁之編號、遮罩標題與網址 | 無 |
| `browser_switch_tab` | **否** | 切換至指定分頁編號（切換前先驗證目標網址政策） | `index`（必填 1-based 整數） |
| `browser_close_tab` | **否** | 關閉指定分頁（`0` 代表目前分頁）並回傳接續頁面狀態 | `index`（預設 `0`） |
| `browser_close` | **否** | 關閉瀏覽器實例與 Playwright driver 並重置工作階段 | 無 |

### 2. 安全防禦邊界

- **前置 URL 守衛與協定／SSRF 隔離（`BrowserUrlPolicy`）**：所有導覽與上下文子請求（`context.route`）皆強制限定 `http`／`https`，阻擋含 Userinfo（包含空帳密 `https://@host`）之混淆網址、永久封鎖雲端 Metadata／Link-Local 位址與使用中之 CDP 端點；預設封鎖 Loopback 與內網私有 IP（除非顯式啟用環境變數或列入 `BROWSER_ALLOWED_DOMAINS`），且所有讀寫工具於執行動作**前**先驗證當前頁面網址，違規時重置為 `about:blank`。
- **機密遮罩與集中稽核**：`browser_snapshot` 與 `browser_fill` 自動偵測 `password`、`otp`/`totp`、`api_key` 等敏感欄位並遮罩；頁面文字、標題與網址皆經 `DefaultRedactionPolicy` 與 `safe_cut_index` 保護後回傳；所有操作與攔截事件均寫入 `agent.log` `[AUDIT] [MCP:browser]` 紀錄。
- **連線鎖、生命週期與跨平台提示**：透過 `BrowserSessionAdapter` 依 Event Loop 管理非同步鎖、單一分頁關閉時重用既有瀏覽器、支援 `server_lifespan` 自動清理，並以 `UserPromptStrategy` 提供 Windows／macOS／Linux（TTY）跨平台人工確認。

### 3. 環境變數與配置參數表 (`servers.browser.env`)

| 參數名稱 | 必填 / 選填 | 預設值 | 說明 |
| :--- | :--- | :--- | :--- |
| `BROWSER_CDP_URL` | 選填 | `""` | 連接既有 Chromium CDP 遠端除錯位址（如 `http://localhost:9222`）；留空時自動啟動本機 Edge／Chrome。 |
| `BROWSER_HEADLESS` | 選填 | `"0"` | 設為 `"1"`／`"true"`／`"yes"`／`"on"` 啟用無頭模式（無頭模式下拒絕 `browser_wait_for_user`）。 |
| `BROWSER_ALLOWED_DOMAINS` | 選填 | `""` | 允許存取之網域白名單（逗號分隔，支援精確網域、`*.domain` 與子網域）。 |
| `BROWSER_ALLOW_PRIVATE_NETWORKS` | 選填 | `"false"` | 設為 `"true"` 才允許存取 RFC 1918／ULA 私有 IP 站點。 |
| `BROWSER_ALLOW_LOOPBACK` | 選填 | `"false"` | 設為 `"true"` 才允許存取 `localhost`／`127.0.0.1`／`::1`（仍禁止存取使用中之 CDP 除錯埠）。 |
| `BROWSER_TIMEOUT_MS` | 選填 | `"15000"` | 單次頁面與定位器操作逾時毫秒數（範圍 `3000` ~ `60000`）。 |
