# Pocket Agent 多軌大模型轉接架構 (LLM Engine Adapter)

為了解決不同 LLM 提供者在 API 規格、認證方式與進階功能（例如 Reasoning 推理過程）上的差異，Pocket Agent 採用 **Adapter Pattern（轉接器模式）** 搭配 **EngineFactory**，將底層模型通訊抽象化為統一的 `BaseEngine` 介面與 `AsyncGenerator` 事件流（`EventText`、`EventThinking`、`EventToolCallStart`、`EventUsage` 等），確保上層 CLI 與 Agent 主迴圈能無縫切換供應商。

## 架構設計視覺化

`EngineFactory`（`src/pocket_agent/engines/factory.py`）依據顯式傳入的 `provider` 選擇對應引擎類別：

```mermaid
classDiagram
    class BaseEngine {
        <<interface>>
        +stream_chat(input, tools, ...) AsyncGenerator
        +context_window() int?
    }

    class OpenAIResponsesEngine {
        +stream_chat()
        +context_window() int?
        -支援 openai 與 openai-api
        -原生解析 Responses API 與 reasoning_content
        -回傳實際 input_tokens 用量
    }

    class OpenRouterEngine {
        +stream_chat()
        +context_window() int?
        -支援 openrouter
        -整合 OpenRouter 專屬標頭與推理參數
    }

    class OllamaChatEngine {
        +stream_chat()
        +context_window() int?
        -支援 ollama 與 ollamacloud
        -內建 State Machine 解析 think 標籤
        -回傳 prompt_tokens 用量
    }

    class CerebrasEngine {
        +stream_chat()
        +context_window() int?
        -支援 cerebras
        -處理 reasoning_effort 與 parsed 推理串流
        -探測 public models 視窗 (max_context_length)
    }

    class GroqEngine {
        +stream_chat()
        +context_window() int?
        -支援 groq (含 stream_options 用量)
        -處理 reasoning_effort 與 parsed 推理串流
        -探測 /models 視窗 (context_window)
    }

    class AzureEngine {
        +stream_chat()
        -支援 azure (含 stream_options 用量)
        -傳遞 low / medium / high 推理強度
    }

    class MistralEngine {
        +stream_chat()
        +context_window() int?
        -支援 mistral
        -探測 /models 視窗 (max_context_length)
    }

    class SambaNovaEngine {
        +stream_chat()
        -支援 sambanova (含 stream_options 用量)
        -傳遞 low / medium / high 推理強度
    }

    class StandardChatCompletionsEngine {
        +stream_chat()
        +context_window() int?
        +parse_api_error() str?
        -支援 gemini / gemini_oauth / openai-compatible
        -共用 delta.reasoning、ThinkTag 狀態機、429 標頭與視窗探測基底
    }

    class EngineFactory {
        +create(provider, client, model) BaseEngine
    }

    BaseEngine <|-- OpenAIResponsesEngine
    BaseEngine <|-- OpenRouterEngine
    BaseEngine <|-- OllamaChatEngine
    BaseEngine <|-- StandardChatCompletionsEngine
    StandardChatCompletionsEngine <|-- CerebrasEngine
    StandardChatCompletionsEngine <|-- GroqEngine
    StandardChatCompletionsEngine <|-- AzureEngine
    StandardChatCompletionsEngine <|-- MistralEngine
    StandardChatCompletionsEngine <|-- SambaNovaEngine
    EngineFactory --> BaseEngine: 根據 provider 建立實體
```

## `<think>` 標籤狀態機剖析機制 (State Machine)

OpenAI 等雲端模型原生支援獨立的推理區塊（Reasoning Content），但部分本地端開源模型（例如 DeepSeek-R1）會將思考過程以 `<think>...</think>` 標籤包裹，混在純文字回覆中一併串流回傳。

為此，`OllamaChatEngine` 內建了**串流狀態機 (Streaming State Machine)**，在收到每個文字區塊 (chunk) 時即時剝離思考內容與正式回覆，使 CLI 能獨立折疊或渲染思考過程，不污染主對話輸出：

```mermaid
flowchart TD
    Chunk[接收 LLM Streaming Chunk] --> IsInThink{目前是否在思考狀態?<br>in_think_block}

    IsInThink -- 否 (False) --> CheckThinkStart{文字包含 &lt;think&gt; 嗎?}
    CheckThinkStart -- 是 --> YieldTextBefore[送出 &lt;think&gt; 前的正式文字]
    YieldTextBefore --> SetThinkTrue["切換至思考狀態 (True)"]
    SetThinkTrue --> ProcessThinkData[處理剩下的思考內容]
    CheckThinkStart -- 否 --> YieldText["送出 EventText (正式文字)"]

    IsInThink -- 是 (True) --> CheckThinkEnd{文字包含 &lt;/think&gt; 嗎?}
    CheckThinkEnd -- 是 --> YieldThinkBefore[送出 &lt;/think&gt; 前的思考文字]
    YieldThinkBefore --> SetThinkFalse["切換回正式狀態 (False)"]
    SetThinkFalse --> ProcessTextData[處理剩下的正式內容]
    CheckThinkEnd -- 否 --> YieldThink["送出 EventThinking (思考文字)"]
```

## 關鍵優勢

1. **無縫切換**：使用者可透過 `/model` 指令即時切換雲端（OpenAI / OpenRouter / Cerebras / Gemini / Groq / Mistral / SambaNova / Azure / Ollama Cloud）或本機（Ollama / OpenAI-Compatible）模型，上層渲染與工具呼叫邏輯完全不需修改。
2. **平滑相容**：無論底層是否原生支援分離式 Reasoning 欄位，轉接器皆統一收斂為 `EventThinking` 事件，提供一致的使用者體驗。
3. **職責分離與擴充性**：底層連線與憑證由 `Provider` 策略負責，協定與串流解析由 `Engine` 負責。常見的 HTTP 429 限流標頭解析（`BaseEngine.parse_rate_limit_headers`）、被動回應標頭額度快照（`supports_header_quota`／`rate_limits.py`）與 `/models` 視窗探測（`StandardChatCompletionsEngine._fetch_context_window_from_models`）已集中於基底類別，各雲端供應商僅透過薄子類別宣告差異參數，新增標準相容服務只需實作 `Provider`（必要時加上薄 `Engine` 子類別）並註冊於 `ENGINE_MAP` 即可。
