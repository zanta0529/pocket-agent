# 敏感設定加密與 OS Keyring 整合

PocketAgent 針對 API Key 與敏感憑證等機敏資料，實作了嚴格的雙軌制保護機制。系統在處理這些機敏設定時，不僅遵循職責分離 (Decoupling) 的架構設計，更採用了符合業界頂規的安全儲存標準。

## 1. 雙軌制安全儲存架構

系統會在讀寫敏感設定時，依序透過以下責任鏈 (Chain of Responsibility) 進行攔截：

1. **作業系統原生憑證管理 (OS Keyring)**【預設首選】
2. **JWE 本機加密庫 (Portable Mode)**【備用降級方案】

### 1.1 OS Keyring 整合

當您在具備圖形化桌面環境的作業系統 (Windows, macOS, 大多數 Linux Desktop) 上運行 PocketAgent 時，系統會優先透過 `keyring` 套件，將 API Key 等敏感資訊寫入作業系統層級的憑證管理員 (如 Windows Credential Locker 或 macOS Keychain)。

- **優點**：與系統登入狀態綁定，開發者在日常操作中可享受**免密碼**的流暢體驗，同時保證了資料不會在磁碟中以明碼存放。
- **佔位符機制**：實體設定檔 `~/.pocket-agent/config.json` 中對應的欄位將只會留下 `"USE_KEYRING"` 的字串作為佔位符，保護實體檔案不洩漏金鑰。

### 1.2 JWE (AES-256-GCM) 降級保護

若您將 PocketAgent 部署在無外接桌面的 Linux Server (Headless) 或啟用了可攜式 (Portable) 隨身碟模式，系統將平滑地切換至本機加密庫模式。

我們實作了符合 RFC 7516 規範的 **JWE (JSON Web Encryption) Compact Serialization** 機制：

- **金鑰衍生 (Argon2id)**：將使用者的主密碼與隨機 Salt 透過 Argon2id (OWASP 推薦標準) 衍生為 256-bit 的內容加密金鑰 (CEK)。
- **對稱加密 (AES-256-GCM)**：採用 AES-256-GCM AEAD (認證加密) 演算法進行加解密。
- **防竄改 (AAD)**：將包含 Argon2 運算成本 (Iterations, Memory, Parallelism) 的 JWE Header 作為附加驗證資料 (AAD) 傳入。任何試圖竄改運算成本以利暴力破解的行為，都會直接在解密階段觸發 `InvalidTag` 錯誤並被拒絕。
- **儲存格式**：密文將獨立存放在 `~/.pocket-agent/secrets.json`，且只包含一串合法的 5 段式 JWE 字串 (例如 `eyJhbG...`)。

## 2. 延遲載入 (Lazy Loading) 與優雅降級

為確保 CLI 操作體驗與錯誤邊界的封裝，我們採用了「延遲載入」策略：

1. **核心不阻塞**：程式啟動時不會要求解鎖，讓不需要 API Key 的本機指令 (如管理對話紀錄、操作免憑證的 Ollama 模型) 維持暢通。
2. **攔截與提示**：只有當特定 LLM Engine 初始化需要存取金鑰，且偵測到 Keyring 無法讀取時，核心才會拋出 `AuthException`。
3. **前端互動**：CLI 前端攔截到該例外後，會暫停背景動畫，跳出隱藏字元的密碼輸入框 (`rich.prompt`) 讓使用者解鎖。
4. **有限重試**：若密碼錯誤或金鑰損毀，系統提供至多 3 次的重試機會，若依然失敗則優雅退出，避免印出雜亂的錯誤追蹤 (Stack Trace)。
