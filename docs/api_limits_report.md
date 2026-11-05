# Pocket Agent 支援平台免費額度與限制分析報告（最後更新日：2026/9/29）

針對本專案支援的各大遠端 LLM 供應商，以下為其「開發者免費層級」的限制與主力模型清單比較。

---

## 1. Ollama Cloud (官方雲端託管)

Ollama 官方推出的雲端服務，目前針對註冊用戶提供專屬的「內含免費額度 (Included usage - Free)」，可免除自建機器的硬體門檻，直接呼叫雲端模型與特殊能力 (例如 Web Search)。

- **收費機制**：提供基礎的免費使用額度 (Free usage credits)。若需無限制使用全庫模型，可選擇 Pay-as-you-go (儲值額度) 或 Upgrade 訂閱制。
- **支援的免費模型**：
  - **Google Gemma 系列**：`gemma4:31b`
  - **開源大模型 (OSS)**：`gpt-oss:120b`, `gpt-oss:20b`
  - **NVIDIA Nemotron 系列**：`nemotron-3-nano:30b`, `nemotron-3-super`, `nemotron-3-ultra`
- **特殊能力**：免費額度不僅可用於純文字生成，亦支援消耗額度來使用附帶功能（例如：Web Search）。

---

## 2. Groq Cloud (On-Demand Tier)

Groq 以極速 LPU 推理聞名，但對免費使用者的瞬間請求 (TPM) 限制極為嚴苛，容易在傳送大檔案時觸發 `429` 錯誤。

- **速率限制 (Rate Limits)**：
  - **Llama 3.3 70B**：30 RPM / 12,000 TPM / 1,000 RPD
  - **Llama 3.1 8B**：30 RPM / 6,000 TPM / 14,400 RPD
  - **Qwen 32B**：60 RPM / 6,000 TPM / 1,000 RPD
- **主力可用模型**：`llama-3.3-70b-versatile`, `llama-3.1-8b-instant`, `qwen-2.5-32b`, `mixtral-8x7b-32768`
- **隱私與備註**：快取的 Token 不計入 TPM。限制會頻繁動態調整，需隨時查看 Console。

---

## 3. SambaNova Cloud

⚠️ **【重要更正】** 根據最新的 2026 年政策，SambaNova 已**取消無條件的永久免費額度**。

- **收費機制**：目前已無「免綁卡/免儲值」的免費測試層（Free Tier）。所有使用者都必須**綁定信用卡並儲值 (Purchase credits)** 升級為 Developer Tier 後，才能開始呼叫 API。
- **速率限制 (Rate Limits)**：需視您的儲值與帳戶等級而定，通常為 Pay-as-you-go。
- **主力可用模型**：`Meta-Llama-3.1-405B-Instruct`, `DeepSeek` 等。

---

## 4. Google Gemini API (Google AI Studio)

Google 提供了極為慷慨的免費層額度，但必須注意**資料隱私**（免費層的對話資料可能會被用於訓練）。限制綁定於 GCP Project。

- **速率限制 (Rate Limits)**：
  - **Gemini Flash 系列 (如 2.x / 3.x Flash)**：15 RPM / **1,000,000 TPM** / 1,500 RPD
  - **Gemini Pro 系列**：大部分高階 Pro 模型在 2026 年已移至付費制，免費層偶有實驗預覽版（如 2 RPM / 32,000 TPM / 50 RPD）。
- **主力可用模型**：`gemini-3.x-flash`, `gemini-2.5-flash`, 以及實驗性質的預覽模型。

---

## 5. Mistral API (Le Free Tier)

歐洲 AI 巨頭的官方 API (La Plateforme)。雖為免費測試層，但幾乎開放了全線商用模型。

- **速率限制 (Rate Limits)**：
  - 動態配發，一般基準約為 **60 RPM** 與 **500,000 TPM**。
  - 無硬性 RPD 限制，但會受限於每月總額度 (Monthly Quota)。
- **主力可用模型**：`mistral-large-latest`, `mistral-small-latest`, `open-mixtral-8x22b`, `codestral` (程式特化)。
- **隱私與備註**：無 SLA 保障，免費層資料可能會被用來改善模型，不建議用於包含機敏資訊的正式生產環境。

---

## 6. OpenRouter (Free Models)

OpenRouter 本身是模型聚合器，它有一個特殊的 `:free` 標籤，提供許多上游免付費的模型。

- **速率限制 (Rate Limits)**：
  - **歷史儲值 < $10**：上限 **50 次 / 天**。
  - **歷史儲值 >= $10**：上限躍升至 **1,000 次 / 天**。
  - **頻率上限**：無論儲值與否，皆被限制為 **20 RPM**。
- **主力可用模型**：`openrouter/free` (自動路由), Llama 系列免費版, Qwen Coder, DeepSeek 實驗版等。
- **注意事項**：帳戶餘額必須為 `>= 0`，若為負值即使呼叫免費模型也會被拒絕 (`402 Error`)。

---

## 7. OpenAI API & ChatGPT Plus (OAuth)

本專案原生支援的 OpenAI 與 OAuth 模式機制差異極大。

- **OpenAI API (傳統 API Key)**：
  - 目前**無免費試用額度**（需綁定信用卡預付至少 5 美元）。
  - 唯一的免費途徑是加入「資料共享計畫」來換取少量的 o3 或 gpt-5 每日額度。
- **ChatGPT Plus (OAuth 模式)**：
  - **雙重動態限制**：採用「5 小時區間」與「每週總用量」雙重計算。
  - 若超過配額，系統會暫時剝奪高階模型存取權，或強制降級至 `GPT-4o mini` 直到下週期重置。
- **主力可用模型**：`GPT-6 Astra/Sol`, `o3`, `o4-mini`, `GPT-5.3 Codex` 等最新系列。

---

> 💡 **總結建議**
>
> - **文件與程式碼分析 (需要百萬 Context Window)**：首選 `Gemini Flash 系列 (如 3.x)` (高達 100 萬 TPM)。
> - **開源模型與快速測試**：使用 `Mistral` 的 Le Free 方案，或 `OpenRouter` 的免費路由。
> - **邏輯推演與免 API 費用**：維持使用本專案的強項 `ChatGPT Plus OAuth` 模式。
