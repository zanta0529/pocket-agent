# RHEL 遠端維運診斷 MCP 伺服器（`rhel-ops`）

本套件為 PocketAgent 隨附之遠端 Red Hat Enterprise Linux（RHEL）唯讀探查與維運診斷 MCP 伺服器，透過本機 OpenSSH `ssh` 執行固定模板的安全診斷指令，支援原始碼開發模式、Portable 免安裝版與 PyInstaller 單一執行檔（`--internal-mcp-server`）。

---

## 模組架構

遵循「共用基礎設施（`_shared/`）＋ 領域子套件（`rhel/`）＋ 頂層薄入口（`rhel_mcp.py`）」三層設計：

| 路徑                                     | 職責說明                                                                                            |
| :--------------------------------------- | :-------------------------------------------------------------------------------------------------- |
| `mcp_servers/rhel_mcp.py`                | Stdio MCP 薄入口，維護啟動契約與對外匯出清單                                                        |
| `mcp_servers/rhel/server.py`             | 透過 `create_mcp_server("rhel-ops", ...)` 組裝並註冊工具群組                                        |
| `mcp_servers/rhel/ssh_client.py`         | SSH 雙軌認證轉接器（帳密／私鑰＋ Passphrase 偵測）、`known_hosts` 同步、1 MB 安全串流擷取與集中稽核 |
| `mcp_servers/rhel/security.py`           | 參數驗證、遠端 `realpath -e` 路徑守衛、`RhelRedactionPolicy` fail-closed 遮罩與安全截斷             |
| `mcp_servers/rhel/config_parsers.py`     | 白名單設定檔結構化安全摘要策略（`BaseConfigStrategy`）                                              |
| `mcp_servers/rhel/tools/system.py`       | 系統概況、儲存與 LVM／RAID、套件、容器、排程與硬體工具                                              |
| `mcp_servers/rhel/tools/network.py`      | 網路介面、路由表、封包統計與 Socket 連線工具                                                        |
| `mcp_servers/rhel/tools/services.py`     | `systemd` 服務、`journald` 等級計數與行程探查工具                                                   |
| `mcp_servers/rhel/tools/security_ops.py` | SELinux、Firewalld 與白名單設定檔摘要工具                                                           |
| `mcp_servers/rhel/tools/diagnostics.py`  | 單次往返綜合健檢、核心異常水位與憑證到期巡檢工具                                                    |

---

## 快速開始與 `mcp_config.yaml` 設定

在專案設定檔 `mcp_config.yaml` 的 `servers:` 區段加入以下設定（所有工具皆為唯讀操作，建議列入 `safe_read_tools`）：

```yaml
servers:
  rhel_ops:
    transport: stdio
    command: python
    args: ['mcp_servers/rhel_mcp.py']
    safe_read_tools:
      - rhel_system_overview
      - rhel_health_snapshot
      - rhel_kernel_health
      - rhel_storage_and_fs
      - rhel_network_status
      - rhel_service_status
      - rhel_query_journal
      - rhel_security_status
      - rhel_read_config
      - rhel_selinux_context
      - rhel_cert_expiry
      - rhel_process_inspect
      - rhel_network_connections
      - rhel_package_info
      - rhel_container_status
      - rhel_cron_and_timers
      - rhel_hardware_info
    env:
      RHEL_HOST: '' # 目標主機 IPv4 / IPv6 / Hostname（留空將於執行時詢問）
      RHEL_SSH_PORT: '' # SSH 連接埠（留空將於執行時詢問，預設 22）
      RHEL_SSH_USER: '' # SSH 登入帳號（留空將於執行時詢問）
      RHEL_SSH_KEY_PATH: '' # 選填：SSH 私鑰路徑（設定時優先啟用金鑰驗證）
      RHEL_SSH_KEY_PASSPHRASE: '' # 選填：私鑰 Passphrase（加密私鑰留空時會自動偵測並安全詢問）
      RHEL_SSH_PASSWORD: '' # 帳密模式密碼（建議留空，於執行期安全輸入不落盤）
      RHEL_STRICT_HOST_KEY: 'accept-new' # 主機金鑰檢查策略（accept-new 或 yes）
      RHEL_CONNECT_TIMEOUT: '10' # SSH 連線逾時秒數（3 ~ 60）
      RHEL_CMD_TIMEOUT: '' # 選填：遠端指令逾時秒數（5 ~ 120，留空預設連線逾時 + 15 秒）
```

### 環境變數說明

| 參數名稱                  | 預設值         | 說明                                                                                |
| :------------------------ | :------------- | :---------------------------------------------------------------------------------- |
| `RHEL_HOST`               | `""`           | 目標 RHEL 主機 IP（支援 IPv4／IPv6）或主機名稱；留空時於首次呼叫工具互動詢問        |
| `RHEL_SSH_PORT`           | `"22"`         | SSH 連接埠（`1` ~ `65535`）                                                         |
| `RHEL_SSH_USER`           | `""`           | SSH 登入帳號                                                                        |
| `RHEL_SSH_PASSWORD`       | `""`           | 帳密模式密碼；留空將於終端機以隱藏輸入詢問並暫存於記憶體（10 分鐘 TTL）             |
| `RHEL_SSH_KEY_PATH`       | `""`           | 指定私鑰檔案路徑；設定後自動切換為 `publickey` 驗證（搭配 `IdentitiesOnly=yes`）    |
| `RHEL_SSH_KEY_PASSPHRASE` | `""`           | 加密私鑰 Passphrase；未設定且系統偵測到私鑰已加密時，會自動於執行期互動詢問         |
| `RHEL_CONNECT_TIMEOUT`    | `"10"`         | SSH 連線階段逾時秒數（夾緊於 `3` ~ `60` 秒）                                        |
| `RHEL_CMD_TIMEOUT`        | `""`           | 遠端指令執行逾時秒數（夾緊於 `5` ~ `120` 秒；留空預設 `RHEL_CONNECT_TIMEOUT + 15`） |
| `RHEL_STRICT_HOST_KEY`    | `"accept-new"` | 設為 `yes`／`strict`／`true`／`1` 時強制啟用 `StrictHostKeyChecking=yes`            |

啟動 PocketAgent 後，可在 CLI 輸入 `/mcp` 確認 `rhel_ops` 已掛載 17 個工具，或輸入 `/mcp rhel_health_snapshot` 檢視單一工具說明。

---

## 工具清單速查（共 17 項唯讀工具）

### 1. 綜合健檢與系統核心

| 工具名稱               | 用途說明                                                                                                                                           | 主要參數                         |
| :--------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------- |
| `rhel_health_snapshot` | 單次 SSH 往返綜合健檢：彙整 OS／Kernel、記憶體／PSI、磁碟／Inode、失敗服務、SELinux／Firewalld 與近 1 小時 `err` 以上日誌計數（無原文）            | 無                               |
| `rhel_system_overview` | 取得 OS 版本、已安裝／預設核心、Uptime、時間同步（`timedatectl`/`chronyc`）、Tuned Profile、CPU／IO Wait 與 PSI、記憶體及前 10 大 CPU 行程         | 無                               |
| `rhel_kernel_health`   | 探查 `/proc/sys/kernel/tainted` 位元解碼、`oom_kill` 次數、`/proc/sys/fs/file-nr`、`/proc/sys/kernel/threads-max` 與核心警告級別日誌計數（無原文） | `since`（預設 `"24 hours ago"`） |
| `rhel_hardware_info`   | 探查虛擬化平台（`systemd-detect-virt`）、CPU 架構（`lscpu`）與記憶體／Swap 明細                                                                    | 無                               |

### 2. 儲存、套件、容器與排程

| 工具名稱                | 用途說明                                                                                                                                              | 主要參數                                                     |
| :---------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------- | :----------------------------------------------------------- |
| `rhel_storage_and_fs`   | 檢查 `df -hT`、Inodes (`df -i`)、區塊裝置 (`lsblk -e 7` 含 `TYPE`/`ROTA`)、LVM VG／LV（含 Thin Pool 使用率）、`/proc/mdstat` 與掛載安全選項           | 無                                                           |
| `rhel_package_info`     | 唯讀查詢本機 RPM 資料庫：單一套件資訊 (`rpm -qi`) 與完整性驗證 (`rpm -V --noscripts`)、近期異動套件 (`recent_count`) 或關鍵字搜尋 (`search`)          | `package_name`、`verify_integrity`、`recent_count`、`search` |
| `rhel_container_status` | 檢查容器服務 `systemd` 狀態與單元，並僅於既有 `docker.service`／`podman.socket` 已啟動時查詢最小容器欄位（`ID`／`Image`／`State`／`Status`／`Ports`） | 無                                                           |
| `rhel_cron_and_timers`  | 查詢 `systemd` timers 排程與啟用狀態（遮罩 `@instance` 為 `@*`）及 `/etc/crontab` 項目計數（缺檔自動歸零，不回傳命令原文）                            | 無                                                           |

### 3. 網路與服務診斷

| 工具名稱                   | 用途說明                                                                                                                                   | 主要參數                                                                 |
| :------------------------- | :----------------------------------------------------------------------------------------------------------------------------------------- | :----------------------------------------------------------------------- |
| `rhel_network_status`      | 檢查介面位址 (`ip -br addr`)、RX／TX 錯誤統計 (`ip -s -br link`)、IPv4／IPv6 路由表、DNS (`/etc/resolv.conf`) 與監聽埠及行程 (`ss -tulnp`) | 無                                                                       |
| `rhel_network_connections` | 利用 `ss` 查詢連線狀態；支援 `summary_only=True` 執行 `ss -s` 統計摘要，或依協議、狀態與 Port 過濾                                         | `protocol`、`state`、`port`、`show_process`、`max_lines`、`summary_only` |
| `rhel_service_status`      | 查詢 `systemd` 服務；未指定名稱或 `failed_only=True` 回傳前 50 筆結構化清單，指定時支援一次最多 10 個服務批次查詢屬性                      | `service_name`、`failed_only`                                            |
| `rhel_query_journal`       | 查詢 `journalctl` 有界等級計數摘要（JSON 含 `sample_limit`、`sample_count`、`priorities` 與 `truncated` 旗標，不回傳訊息原文）             | `unit`、`priority`、`since`、`lines`                                     |
| `rhel_process_inspect`     | 指定 PID 檢視狀態摘要、以固定字串大小寫不敏感搜尋行程（保留表頭），或依 CPU／Memory 排序（支援 SELinux 標籤）                              | `pid`、`search`、`sort_by`、`show_selinux`、`max_lines`                  |

### 4. 資安、設定摘要與憑證

| 工具名稱               | 用途說明                                                                                                                                                   | 主要參數                 |
| :--------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------- | :----------------------- |
| `rhel_security_status` | 檢查 SELinux 模式與 Firewalld 預設／作用中區域、Runtime 及 Permanent 規則摘要                                                                              | 無                       |
| `rhel_read_config`     | 僅透過白名單結構化策略摘要 5 個系統設定檔：`/etc/selinux/config`、`/etc/ssh/sshd_config`、`/etc/sysctl.conf`、`/etc/security/limits.conf`、`/etc/fstab`    | `file_path`、`max_lines` |
| `rhel_selinux_context` | 經遠端 `realpath -e` 守衛驗證後，查詢白名單路徑下檔案或目錄的目前 SELinux 標籤 (`ls -dZ`) 與預期標籤 (`matchpathcon`)                                      | `target_path`            |
| `rhel_cert_expiry`     | 巡檢白名單公開憑證目錄（`/etc/pki/tls/certs`、`/etc/pki/ca-trust/source/anchors`、`/etc/nginx/ssl`、`/etc/httpd/conf/ssl.crt`）之 X.509 到期日（排除私鑰） | `cert_dir`               |

---

## 實務維運場景與建議提問範例

| 維運情境                        | 對應工具                                            | 建議對 PocketAgent 提問方式                                                                                                         |
| :------------------------------ | :-------------------------------------------------- | :---------------------------------------------------------------------------------------------------------------------------------- |
| **每日巡檢／事故快篩**          | `rhel_health_snapshot`                              | 「幫我對這台 RHEL 做一次快速健康快照，有異常項目請標出來。」                                                                        |
| **行程消失／效能卡頓**          | `rhel_kernel_health`                                | 「檢查這台主機過去 24 小時有沒有發生過 OOM Kill、Kernel Taint 或開啟檔案數（FD）接近上限。」                                        |
| **LVM Thin Pool／RAID 風險**    | `rhel_storage_and_fs`                               | 「檢查磁碟與 Inode 使用率、LVM VG 剩餘空間、Thin Pool 的 data／metadata 百分比與軟體 RAID 狀態。」                                  |
| **安全基線與效能調校稽核**      | `rhel_read_config`                                  | 「幫我做主機基線稽核：檢查 `/etc/ssh/sshd_config`、`sysctl.conf`、`limits.conf`、`/etc/selinux/config` 與 `/etc/fstab` 掛載選項。」 |
| **預防憑證過期事故**            | `rhel_cert_expiry`                                  | 「幫我巡檢 `/etc/pki/tls/certs`（或 `/etc/nginx/ssl`）裡有沒有即將過期的 X.509 憑證，並依到期日整理清單。」                         |
| **變更追查／套件盤點**          | `rhel_package_info`                                 | 「列出這台主機最近安裝或更新的前 20 個套件。」或「查一下本機已安裝的 `openssl` 相關套件版本。」                                     |
| **Podman／Docker 巡檢**         | `rhel_container_status`                             | 「檢查這台主機的 Podman 與 Docker 服務狀態，以及目前正在運作的容器清單。」                                                          |
| **高併發連線／封包／IPv6 排錯** | `rhel_network_connections` ＋ `rhel_network_status` | 「先用 `ss -s` 看目前 Socket 連線狀態總量統計，並檢查各網卡有沒有 RX／TX 錯誤或 IPv6 路由異常。」                                   |
| **精準行程與日誌截斷判讀**      | `rhel_process_inspect` ＋ `rhel_query_journal`      | 「幫我搜尋 `python3.11` 相關行程，並查詢過去 2 小時 `nginx.service` 的 error 等級日誌統計，確認樣本是否被截斷。」                   |

---

## 建議健檢工作流與安全邊界

1. **先快照、再下鑽**：日常健檢建議先呼叫 `rhel_health_snapshot`（單次 SSH 連線取得全域摘要）；若發現 `failed_units > 0` 再呼叫 `rhel_service_status`（以逗號分隔一次查詢最多 10 個服務），若磁碟、記憶體或系統異常再深入呼叫 `rhel_storage_and_fs` 或 `rhel_kernel_health`，可大幅減少 SSH 握手次數與 Token 消耗。
2. **零全域金鑰與憑證隔離**：SSH 強制附加 `GSSAPIAuthentication=no`、`IdentitiesOnly=yes`、`IdentityAgent=none` 與 `-F none`，不讀取本機 `~/.ssh/config` 或 SSH Agent；連線憑證僅暫存於記憶體（10 分鐘 TTL，連線或認證失敗即自動清除）。
3. **唯讀與遮罩保證**：不提供任何任意 Shell 執行或檔案寫入工具；所有擷取設有 `1 MB` 位元組上限與 `12,000` 字元輸出上限，透過 `RhelRedactionPolicy` 執行 fail-closed 檢查與遮罩（`/etc/fstab` 敏感掛載選項於遠端即替換為 `[REDACTED_OPT]`），且所有操作均寫入 `agent.log` 的 `[AUDIT] [MCP:rhel-ops]` 紀錄，敏感工作階段對話不落盤。
