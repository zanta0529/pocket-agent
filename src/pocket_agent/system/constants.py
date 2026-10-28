"""Pocket Agent 全域系統參數與限制常數中央管理模組 (System Constants & Limits)

本模組為專案中所有數值限制、緩衝區大小、超時時間及預算水位的單一權威來源 (Single Source of Truth)。
所有常數皆附帶設計意圖、適用之模型視窗規模 (特別針對 64k~128k 視窗) 以及安全防護說明。
"""

from dataclasses import dataclass

# ==============================================================================
# 1. 工具輸出字元上限 (Tool Output Character Limits)
# ==============================================================================
# 設計意圖：
# 限制單次工具執行後，回傳給 AI 模型上下文 (Context) 的純文字字元數上限。
# 12,000 字元約等同於 3,000 ~ 4,000 tokens (以 3~4 字元/token 保守計算)。
# 在 64k 視窗中僅佔約 5%，在 128k 視窗中僅佔約 2.5%，既能容納完整長設定檔與堆疊追蹤，
# 又絕不擠爆模型上下文預算。
# ==============================================================================
DEFAULT_TOOL_OUTPUT_CHARS = 12000
MAX_TOOL_OUTPUT_CHARS = 24000
MIN_TOOL_OUTPUT_CHARS = 500


# ==============================================================================
# 2. 作業系統子行程與管線排空緩衝區 (Process Buffer & Anti-DoS Limits)
# ==============================================================================
# 設計意圖：
# 在本機 (shell.py) 與遠端 (rhel_mcp.py) 執行 CLI 或 SSH 子行程時，
# 雙管線 (stdout/stderr) 串流並行排空的累積記憶體位元組上限。
# 超過此門檻時，系統會立即調用 proc.kill() 強制中止行程，杜絕惡意大檔或無窮迴圈
# 消耗主機 RAM 造成 DoS 攻擊。1 MB 緩衝區可充分容納常規編譯輸出與長日誌。
# ==============================================================================
DEFAULT_PROCESS_BUFFER_BYTES = 1024 * 1024  # 1 MB 安全串流排空上限
MAX_READ_FILE_BYTES = 10 * 1024 * 1024  # 10 MB: 本機讀檔保護底線，超過此大小拒絕直接讀入
MAX_FILE_SEARCH_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB: search_files 全文檢索時跳過的大檔閾值
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB: Web 工具爬取網頁時的最大 HTTP 接收大小


# ==============================================================================
# 3. 原生檔案系統操作限制 (File System Limits)
# ==============================================================================
# 設計意圖：
# 控制原生檔案工具 (fs.py) 的檢索精度、預設切片與筆數限制。
# ==============================================================================
DEFAULT_READ_FILE_LINES = 500  # read_file 單次讀取的預設最大行數
DEFAULT_SEARCH_MAX_RESULTS = 100  # search_files 檔名或關鍵字檢索最多回傳筆數
TIME_MACHINE_BACKUP_MAX_BYTES = 20 * 1024 * 1024  # 20 MB: 檔案修改前時光機備份上限


# ==============================================================================
# 4. 網路與外部請求限制 (Network & Crawler Limits)
# ==============================================================================
# 設計意圖：
# 控制爬蟲、網頁讀取與搜尋引擎查詢的重定向、超時與字元長度。
# ==============================================================================
DEFAULT_NETWORK_TIMEOUT_SEC = 15.0  # HTTP 請求逾時秒數
DEFAULT_MAX_REDIRECTS = 5  # 防範重新導向循環 (Redirect Loop) 的跳轉次數上限
DEFAULT_WEB_CONTENT_MAX_CHARS = 15000  # read_url_content 抽取純文字後回傳模型的字元上限
DEFAULT_STREAM_READ_TIMEOUT_SEC = 120.0  # LLM SSE 串流單區塊最大閒置讀取逾時秒數


# ==============================================================================
# 5. 上下文預算與溢出管理常數 (Context Budget & Spillover Defaults)
# ==============================================================================
# 設計意圖：
# 控制模型視窗、歷史高密度摘要壓縮、以及巨量日誌雙端切片溢出 (Spillover)。
# ==============================================================================
DEFAULT_CONTEXT_WINDOW = 128_000  # 未知模型時的保守保底視窗 (tokens)
DEFAULT_RESERVE_OUTPUT_TOKENS = 4096  # 預留給模型輸出與推理鏈的空間 (tokens)
DEFAULT_SAFETY_MARGIN_TOKENS = 1024  # 上下文估算誤差防範餘裕 (tokens)
DEFAULT_COMPACT_RATIO = 0.7  # 觸發清除舊工具輸出的水位 (佔有效預算之 70%)
DEFAULT_COMPACT_TARGET_RATIO = 0.6  # 清理後的目標降載水位 (佔有效預算之 60%)
DEFAULT_TOOL_OUTPUT_RATIO = 0.08  # 單筆工具輸出佔有效視窗之建議上限 (8%)
CHARS_PER_TOKEN_ESTIMATE = 3  # 無 tokenizer 時的保守字元/Token 換算比率
DEFAULT_KEEP_RECENT_OUTPUTS = 2  # 清除歷史時強制保留的原樣工具輸出筆數
DEFAULT_SPILLOVER_HEAD_CHARS = 1500  # 輸出溢出落盤時保留的前端啟動日誌字元數
DEFAULT_SPILLOVER_TAIL_CHARS = 1500  # 輸出溢出落盤時保留的尾端測試結論字元數
DEFAULT_COMPACTION_KEEP_MESSAGES = 6  # /compact 摘要時保留的最近對話訊息數
DEFAULT_REPO_MAP_BUDGET_TOKENS = 1024  # AST 鳥瞰地圖佔用的 Token 預算
DEFAULT_RAG_TOP_K = 5  # 本機 FTS5 檢索預設回傳代碼片段數
MAX_RAG_TOP_K = 10  # 本機 FTS5 檢索最大片段數


# ==============================================================================
# 6. Agent 執行週期與日誌安全 (Agent Lifecycle & Safety)
# ==============================================================================
# 設計意圖：
# 控制工具呼叫輪次、調研防發散限制以及本機日誌自動輪轉大小。
# ==============================================================================
DEFAULT_MAX_TOOL_ROUNDS = 20  # 單次回合允許工具自主呼叫之最大輪次
DEFAULT_MAX_STREAM_RETRIES = 3  # 串流連線異常時允許之最大重試次數
DEFAULT_STREAM_RETRY_BASE_SEC = 1.5  # 串流重試之指數退避基礎秒數
DEFAULT_CLI_TIMEOUT_SEC = 60  # 本機 run_command 指令預設超時秒數
MAX_CLI_TIMEOUT_SEC = 600  # 本機 run_command 最長允許超時秒數 (10 分鐘)
WEB_RESEARCH_CHECKPOINT_INTERVAL = 3  # 網路檢索觸發 Scratchpad 階段性筆記提醒之間隔次數
WEB_RESEARCH_SOFT_LIMIT = 5  # 網路檢索軟性收斂提醒閾值 (累計次數)
WEB_RESEARCH_HARD_LIMIT = 10  # 網路檢索硬性攔截上限 (累計次數)
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB: agent.log 滾動備份上限
LOG_BACKUP_COUNT = 3  # agent.log 保留歷史份數
CLI_MAX_AUTH_RETRIES = 3  # CLI 金鑰密碼解鎖重試次數上限


# ==============================================================================
# 7. AI 模型供應商識別常數 (Model Provider Identifiers)
# ==============================================================================
# 設計意圖：
# 集中管理各 LLM 供應商的唯一標識字串，供 EngineFactory、組態設定與路由分派使用。
# ==============================================================================
PROVIDER_GEMINI = "gemini"
PROVIDER_GEMINI_OAUTH = "gemini_oauth"
PROVIDER_GROQ = "groq"
PROVIDER_MISTRAL = "mistral"
PROVIDER_OLLAMA = "ollama"
PROVIDER_OLLAMACLOUD = "ollamacloud"
PROVIDER_OPENAI = "openai"
PROVIDER_OPENAI_API = "openai-api"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_SAMBANOVA = "sambanova"


@dataclass(frozen=True)
class SystemConstants:
    """系統常數集中存取容器 (唯讀結構)"""

    tool_output_chars: int = DEFAULT_TOOL_OUTPUT_CHARS
    process_buffer_bytes: int = DEFAULT_PROCESS_BUFFER_BYTES
    max_read_file_bytes: int = MAX_READ_FILE_BYTES
    read_file_lines: int = DEFAULT_READ_FILE_LINES
    read_file_chars: int = DEFAULT_TOOL_OUTPUT_CHARS
    search_max_results: int = DEFAULT_SEARCH_MAX_RESULTS
    network_timeout_sec: float = DEFAULT_NETWORK_TIMEOUT_SEC
    network_max_redirects: int = DEFAULT_MAX_REDIRECTS
    web_content_max_chars: int = DEFAULT_WEB_CONTENT_MAX_CHARS
    context_window: int = DEFAULT_CONTEXT_WINDOW
    reserve_output: int = DEFAULT_RESERVE_OUTPUT_TOKENS
    safety_margin: int = DEFAULT_SAFETY_MARGIN_TOKENS
    compact_ratio: float = DEFAULT_COMPACT_RATIO
    compact_target_ratio: float = DEFAULT_COMPACT_TARGET_RATIO
    tool_output_ratio: float = DEFAULT_TOOL_OUTPUT_RATIO
    spillover_head_chars: int = DEFAULT_SPILLOVER_HEAD_CHARS
    spillover_tail_chars: int = DEFAULT_SPILLOVER_TAIL_CHARS
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS
    cli_timeout_sec: int = DEFAULT_CLI_TIMEOUT_SEC
    max_cli_timeout_sec: int = MAX_CLI_TIMEOUT_SEC
