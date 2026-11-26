# 網站與網路跨單位舉證診斷 MCP 伺服器（`netdiag`）

本套件為 PocketAgent 隨附之網站與網路跨單位舉證診斷 MCP 伺服器，專為權責分離環境下的網站負責人設計，提供唯讀、因果連貫的階段式網路舉證報告（`DNS → TCP → TLS 憑證鏈 → HTTP`）與多解析器 DNS 比對，預設停用（`enabled: false`）且**強制逐次人工確認**。

---

## 模組架構

遵循「共用基礎設施（`_shared/`）＋ 領域子套件（`netdiag/`）＋ 頂層薄入口（`netdiag_mcp.py`）」三層設計：

| 路徑 | 職責說明 |
| :--- | :--- |
| `mcp_servers/netdiag_mcp.py` | Stdio MCP 薄入口，維護啟動契約與對外匯出清單 |
| `mcp_servers/netdiag/server.py` | 透過 `create_mcp_server("netdiag", ...)` 組裝並註冊工具群組 |
| `mcp_servers/netdiag/models.py` | 不可變資料模型（`NetdiagConfig`、`ValidatedTarget`、各階段資料、`DiagnosticReport`、`DnsCompareReport`） |
| `mcp_servers/netdiag/policy.py` | 安全授權與網段策略：授權先於 DNS、硬封鎖保留／Metadata 網段、公私網分層與 Proxy Fail-Closed |
| `mcp_servers/netdiag/redaction.py` | `NetdiagRedactionPolicy`：URL 帳密與敏感 Query 遮罩、白名單回應標頭、僅摘要 `Set-Cookie` 旗標 |
| `mcp_servers/netdiag/dns_adapter.py` | DNS 轉接層：延遲載入 `dnspython`，缺套件時降級標準庫 `socket` |
| `mcp_servers/netdiag/tls_probe.py` | Stage 3 TLS 併發雙軌 CA（`truststore` vs `certifi`）驗證與零 HTTP 資料純握手憑證舉證 |
| `mcp_servers/netdiag/http_probe.py` | Stage 4 唯讀 `HEAD`／`GET` 探測、Pinned IP 連線與手動逐跳轉址重審（含 `http -> https` TLS 檢核） |
| `mcp_servers/netdiag/signals.py` | 可插拔診斷訊號分析策略（Proxy／雙棧、TLS 臨期與弱協定、邊緣指紋、HTTPS 安全標頭與協查指引） |
| `mcp_servers/netdiag/pipeline.py` | Stage 0→4 短路編排管線、雙棧 IP 過濾、採樣統計與 `[AUDIT] [MCP:netdiag]` 稽核 |
| `mcp_servers/netdiag/dns_compare.py` | 多解析器 DNS 比對與 SPF／`_dmarc.<domain>`／CAA 治理訊號編排器 |
| `mcp_servers/netdiag/report.py` | 報告格式化策略（`EscalationMarkdownFormatter` 與 `JsonReportFormatter`） |
| `mcp_servers/netdiag/factory.py` | `NetdiagServiceFactory` 服務組裝工廠與統一參數攔截稽核輔助 |
| `mcp_servers/netdiag/tools/diagnose.py` | `netdiag_diagnose` 工具註冊與參數邊界保護 |
| `mcp_servers/netdiag/tools/dns.py` | `netdiag_dns_compare` 工具註冊與參數邊界保護 |

---

## 快速開始與 `mcp_config.yaml` 設定

在專案設定檔 `mcp_config.yaml` 的 `servers:` 區段加入以下設定（啟用時將 `enabled` 改為 `true`；基於安全政策，**請勿將 `netdiag_*` 加入 `safe_read_tools`**）：

```yaml
servers:
  netdiag:
    enabled: false
    transport: stdio
    command: python
    args: ["mcp_servers/netdiag_mcp.py"]
    # is_sensitive: true                         # 強烈建議：啟用內網私有 IP 診斷時設為 true，強制對話紀錄零落盤
    env:
      NETDIAG_ALLOW_PRIVATE_NETWORKS: "false"    # 設為 true 允許診斷內網 RFC 1918 / ULA 私有 IP
      NETDIAG_ALLOWED_CIDRS: ""                  # 選填：留空且開啟內網時允許所有 RFC 1918 / ULA；設定時限縮於指定 CIDR
      NETDIAG_ALLOWED_DOMAINS: ""                # 選填：限制僅允許診斷指定網域（逗號分隔，支援 "*.example.org"）
      NETDIAG_ALLOWED_PORTS: "80,443,8080,8443"  # 允許探測之連接埠白名單
      NETDIAG_ALLOW_LOOPBACK: "false"            # 預設封鎖 localhost / 127.0.0.1 / ::1
      NETDIAG_ALLOW_PROXY: "false"               # 預設 fail-closed 拒用環境 Proxy（企業代理環境請顯式設為 "true"）
      NETDIAG_TIMEOUT: "10"                      # 單階段逾時秒數（3 ~ 15）
```

### 環境變數與依賴說明

| 參數名稱 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `NETDIAG_ALLOW_PRIVATE_NETWORKS` | `"false"` | 設為 `"true"` 才允許診斷內網 RFC 1918／ULA 私有 IP |
| `NETDIAG_ALLOWED_CIDRS` | `""` | 啟用內網時選填之管轄 CIDR 白名單（留空則允許所有 RFC 1918／ULA） |
| `NETDIAG_ALLOWED_DOMAINS` | `""` | 允許診斷之網域白名單（逗號分隔，支援 `*.example.org`） |
| `NETDIAG_ALLOWED_PORTS` | `"80,443,8080,8443"` | 允許探測之連接埠白名單（`1` ~ `65535`） |
| `NETDIAG_ALLOW_LOOPBACK` | `"false"` | 設為 `"true"` 才允許診斷 `localhost`／`127.0.0.1`／`::1` |
| `NETDIAG_ALLOW_PROXY` | `"false"` | 預設 fail-closed 拒用環境 Proxy；企業代理環境須顯式設為 `"true"` |
| `NETDIAG_TIMEOUT` | `"10"` | 單階段預設逾時秒數（夾緊於 `3.0` ~ `15.0` 秒） |

- **選用／核心依賴**：完整功能使用 `httpx`、`cryptography`、`certifi`、`truststore` 與 `dnspython`；若環境未安裝 `dnspython`，DNS 查詢會自動降級為標準庫 `socket`（僅支援系統預設解析器之 `A`／`AAAA`）。
- **啟動驗證**：將 `enabled` 設為 `true` 並啟動 PocketAgent 後，可於 CLI 輸入 `/mcp` 確認 `netdiag` 已掛載 2 個工具，或輸入 `/mcp netdiag_diagnose` 檢視詳細參數說明。

---

## 工具清單速查（共 2 項，強制逐次確認）

| 工具名稱 | 建議列入 `safe_read_tools` | 用途說明 | 主要參數 |
| :--- | :--- | :--- | :--- |
| `netdiag_diagnose` | **否（強制逐次確認）** | 執行階段式唯讀交易診斷（`DNS → TCP → TLS → HTTP`），標註 `first_failure_stage`、雙軌 CA 對照、憑證臨期／弱協定／安全標頭／邊緣指紋訊號與協查指引，支援 Markdown／JSON 輸出 | `target`、`max_stage`（`dns`/`tcp`/`tls`/`http`）、`method`（`HEAD`/`GET`）、`host_header`、`ip_family`（`auto`/`ipv4`/`ipv6`）、`samples`（`1`~`3`）、`timeout_sec`（`3.0`~`15.0`）、`output_format`（`markdown`/`json`） |
| `netdiag_dns_compare` | **否（強制逐次確認）** | 比對系統預設 DNS 與指定外部解析器之網域紀錄差異、TTL、CNAME 鏈與 SPF／`_dmarc.<domain>`／CAA 紀錄，支援 Markdown／JSON 輸出 | `domain`、`record_types`（`A`/`AAAA`/`CNAME`/`NS`/`SOA`/`MX`/`TXT`/`CAA`/`HTTPS`/`SRV`）、`nameservers`（最多 4 個 IP）、`timeout_sec`、`output_format`（`markdown`/`json`） |

---

## 實務跨單位舉證場景與建議提問範例

| 維運舉證情境 | 對應工具 | 建議對 PocketAgent 提問方式 |
| :--- | :--- | :--- |
| **網站連不上快速界定責任單位** | `netdiag_diagnose` | 「幫我診斷 `https://app.example.com`，找出第一個失敗的階段是 DNS、TCP、TLS 還是 HTTP，並整理給對應單位的協查資訊。」 |
| **憑證異常、臨期與企業 Root CA 對照** | `netdiag_diagnose` | 「針對 `https://portal.example.com` 執行到 `max_stage="tls"`，對照系統 `truststore` 與 `certifi` 的驗證結果並檢查憑證是否低於 30 天到期或使用弱 TLS。」 |
| **雙棧 IPv4／IPv6 獨立隔離排錯** | `netdiag_diagnose` | 「分別以 `ip_family="ipv4"` 與 `ip_family="ipv6"` 診斷 `https://www.example.com`，確認是否僅單一網路棧異常。」 |
| **內網虛擬主機／反向代理除錯** | `netdiag_diagnose` | 「用 `host_header="api.internal.local"` 探測 `https://10.20.30.40`，確認內部主機的 TLS SNI、安全標頭與 HTTP 回應狀態。」 |
| **效能緩慢、轉址鏈與邊緣指紋舉證** | `netdiag_diagnose` | 「對 `https://www.example.com` 使用 `method="GET"` 採樣 3 次，統計 TTFB 的 min／median／max、轉址標頭與 CDN／WAF 邊緣指紋。」 |
| **自動化彙整或 JSON 結構化輸出** | `netdiag_diagnose` / `netdiag_dns_compare` | 「對 `https://api.example.com` 執行診斷並指定 `output_format="json"`，方便我直接對接檢核腳本。」 |
| **Split-Horizon DNS 不一致舉證** | `netdiag_dns_compare` | 「用 `1.1.1.1,8.8.8.8` 和系統預設 DNS 比對 `app.example.com` 的 `A,AAAA,CNAME` 紀錄與 TTL 是否一致。」 |
| **網域安全與郵件治理盤點** | `netdiag_dns_compare` | 「查詢 `example.com` 的 `A,AAAA,MX,TXT,CAA` 紀錄，自動一併檢查 `_dmarc.example.com`、SPF 與 CAA 簽發授權。」 |

---

## 建議舉證工作流與安全防禦邊界

1. **先分階段快篩、再深入採樣**：排查異常建議先以預設 `max_stage="http"`、`method="HEAD"`、`samples=1` 鎖定 `first_failure_stage`；若需統計延遲抖動或檢查轉址鏈，再提高為 `samples=3` 或 `method="GET"`；若問題落在解析階段，再搭配 `netdiag_dns_compare` 比對多個解析器。
2. **授權先於 DNS 與永久硬封鎖**：在任何 DNS 解析前先驗證 URL 協定（僅 `http`／`https`）、禁止內嵌帳密、連接埠白名單與 `NETDIAG_ALLOWED_DOMAINS`；永久封鎖 Link-Local／雲端 Metadata（`169.254.0.0/16`、`fe80::/10`、`fd00:ec2::254`）、Multicast 與未指定位址。
3. **公私網分層、Pinned IP 與 Proxy Fail-Closed**：預設封鎖私有 IP 與 Loopback；採 Pinned IP 連線並於每跳 HTTP 轉址重審目標與網段，防範 DNS Rebinding；偵測到環境 Proxy 且未設 `NETDIAG_ALLOW_PROXY=true` 時立即中止，代理失敗亦絕不降級直連；TLS 驗證失敗時中止 HTTP 階段，僅以零應用層資料之純握手擷取公開憑證欄位。
4. **CLI 強制逐次確認與機密遮罩**：`ConfirmationBridge` 會渲染目標網段、埠口、Host／SNI 與階段預覽，禁止寫入會話或永久白名單；回應經 `NetdiagRedactionPolicy` 遮罩敏感 Query、`Set-Cookie` 實際值與非白名單標頭，所有操作寫入 `agent.log` 的 `[AUDIT] [MCP:netdiag]` 紀錄。
