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
DEFAULT_PROCESS_READ_CHUNK_BYTES = 4096  # 4 KB: 子行程 stdout 單次讀取區塊大小
MAX_READ_FILE_BYTES = 10 * 1024 * 1024  # 10 MB: 本機讀檔保護底線，超過此大小拒絕直接讀入
MAX_IMAGE_INPUT_BYTES = 20 * 1024 * 1024  # 20 MB: /image 單張參考圖最大檔案大小
MAX_IMAGE_INPUT_COUNT = 10  # /image 單次請求最多允許的參考圖數量
MAX_FILE_SEARCH_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB: search_files 全文檢索時跳過的大檔閾值
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB: Web 工具爬取網頁時的最大 HTTP 接收大小

# 資料庫 MCP 健康檢查與安全熔斷限制 (Database MCP Health & Safety Guardrails)
DB_HEALTH_MAX_TABLES = 500  # 單次資料庫健康巡檢最多分析的資料表數量上限
MYSQL_CHECK_TABLE_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB: MySQL CHECK TABLE 允許的最大單表容量上限
SQLITE_QUICK_CHECK_MAX_BYTES = 500 * 1024 * 1024  # 500 MB: SQLite PRAGMA quick_check 允許的最大檔案大小上限
DB_FRAGMENTATION_WARN_RATIO = 0.20  # 資料表空間碎片率預警水位 (20%)
DB_FRAGMENTATION_MIN_FREE_BYTES = 10 * 1024 * 1024  # 10 MB: 觸發資料表碎片率預警的最小閒置空間門檻
DB_AUTOINC_WARN_RATIO = 0.75  # 自動遞增 (AUTO_INCREMENT / IDENTITY) 欄位溢位預警水位 (75%)


# ==============================================================================
# 3. 原生檔案系統操作限制 (File System Limits)
# ==============================================================================
# 設計意圖：
# 控制原生檔案工具 (fs.py) 的檢索精度、預設切片與筆數限制。
# ==============================================================================
DEFAULT_READ_FILE_LINES = 500  # read_file 單次讀取的預設最大行數
DEFAULT_SEARCH_MAX_RESULTS = 100  # search_files 檔名或關鍵字檢索最多回傳筆數
DEFAULT_SEARCH_LINE_MAX_CHARS = 500  # search_files 全文檢索單行命中顯示的最大字元上限
TIME_MACHINE_BACKUP_MAX_BYTES = 20 * 1024 * 1024  # 20 MB: 檔案修改前時光機備份上限
USER_DATA_DIR_NAME = ".pocket-agent"  # 使用者資料根目錄名稱 (~/.pocket-agent)
DEFAULT_SKILL_RESOURCE_LIST_LIMIT = 20  # use_skill 提示中最多列出的頂層附屬資源項目數
TOOL_NOT_FOUND_PREFIX = "[NOT_FOUND]"  # 唯讀探測工具查無目標時的不可翻譯內部狀態前綴
TOOL_OVERWRITE_REQUIRED_PREFIX = "[OVERWRITE_REQUIRED]"  # 自動編輯覆寫既有檔案需顯式 overwrite=true 之內部前綴
TOOL_TARGET_NOT_FOUND_PREFIX = "[TARGET_NOT_FOUND]"  # replace_file_content 查無相符目標之不可翻譯內部狀態前綴
REPLACE_SCOPE_DRIFT_LINES = 20  # replace_file_content 在指定行數範圍附近容許自動對齊的最大上下行數偏移
REPLACE_DIAGNOSTIC_MAX_MATCHES = 5  # replace_file_content 回報實際行號診斷時最多列出的位置數量


# ==============================================================================
# 4. 網路與外部請求限制 (Network & Crawler Limits)
# ==============================================================================
# 設計意圖：
# 控制爬蟲、網頁讀取與搜尋引擎查詢的重定向、超時與字元長度。
# ==============================================================================
DEFAULT_NETWORK_TIMEOUT_SEC = 15.0  # HTTP 請求逾時秒數
DEFAULT_MAX_REDIRECTS = 5  # 防範重新導向循環 (Redirect Loop) 的跳轉次數上限
DEFAULT_WEB_CONTENT_MAX_CHARS = 15000  # read_url_content 抽取純文字後回傳模型的字元上限
DEFAULT_WEB_SEARCH_MAX_RESULTS = 5  # search_web 預設擷取的搜尋結果筆數上限
DEFAULT_WEB_FALLBACK_MAX_RESULTS = 3  # HTTP 404 自動退避搜尋時擷取的建議結果筆數上限
DEFAULT_WEB_STREAM_CHUNK_BYTES = 8192  # Web 工具串流讀取單次區塊大小 (8 KB)
DEFAULT_ALLOWED_SSRF_DOMAINS: tuple[str, ...] = (
    "github.com",
    "raw.githubusercontent.com",
)  # 預設允許企業代理解析之受信任網域
DEFAULT_STREAM_READ_TIMEOUT_SEC = 120.0  # LLM SSE 串流單區塊最大閒置讀取逾時秒數

# 網站與網路診斷 MCP (netdiag) 限流與安全常數
NETDIAG_DEFAULT_TIMEOUT_SEC = 10.0  # netdiag 單階段預設逾時秒數
NETDIAG_MIN_TIMEOUT_SEC = 3.0  # netdiag 允許設定的最小逾時秒數
NETDIAG_MAX_TIMEOUT_SEC = 15.0  # netdiag 允許設定的最大逾時秒數
NETDIAG_MAX_SAMPLES = 3  # netdiag 單次呼叫最多採樣次數
NETDIAG_MAX_HEADER_VALUE_CHARS = 512  # netdiag 單一回應標頭值顯示上限
NETDIAG_DNS_MAX_NAMESERVERS = 4  # netdiag_dns_compare 單次最多比對之額外解析器數量
NETDIAG_DNS_MAX_CNAME_HOPS = 10  # netdiag DNS CNAME 鏈追蹤最大跳數
NETDIAG_DEFAULT_ALLOWED_PORTS: tuple[int, ...] = (80, 443, 8080, 8443)  # netdiag 預設允許探測之連接埠


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
DEFAULT_RAG_MAX_FILE_LINES = 2000  # 本機 FTS5 索引單一原始碼檔案最多擷取行數上限


# ==============================================================================
# 6. Agent 執行週期與日誌安全 (Agent Lifecycle & Safety)
# ==============================================================================
# 設計意圖：
# 控制工具呼叫輪次、調研防發散限制以及本機日誌自動輪轉大小。
# ==============================================================================
DEFAULT_MAX_TOOL_ROUNDS = 20  # 單次回合允許工具自主呼叫之最大輪次
DEFAULT_MAX_LINKED_DOCS_CHARS = 24000  # 啟動時 AGENTS.md / instructions.md 自動展開連結文件之總字元上限
VALID_REASONING_EFFORTS = ("auto", "high", "medium", "low", "none")  # 支援的推理強度選項
DEFAULT_REASONING_EFFORT = "auto"  # 預設交由模型／供應商自行決定，兼顧首字延遲與品質
DEFAULT_MAX_STREAM_RETRIES = 3  # 串流連線異常時允許之最大重試次數
DEFAULT_STREAM_RETRY_BASE_SEC = 1.5  # 串流重試之指數退避基礎秒數
DEFAULT_OPERATION_DRAIN_TIMEOUT_SEC = 2.0  # CLI run_operation 取消與收尾時最大允許等待秒數
DEFAULT_CLI_TIMEOUT_SEC = 60  # 本機 run_command 指令預設超時秒數
MAX_CLI_TIMEOUT_SEC = 600  # 本機 run_command 最長允許超時秒數 (10 分鐘)
WEB_RESEARCH_CHECKPOINT_INTERVAL = 3  # 網路檢索觸發 Scratchpad 階段性筆記提醒之間隔次數
WEB_RESEARCH_SOFT_LIMIT = 5  # 網路檢索軟性收斂提醒閾值 (累計次數)
WEB_RESEARCH_HARD_LIMIT = 10  # 網路檢索硬性攔截上限 (累計次數)
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB: 單日日誌滾動備份上限
LOG_BACKUP_COUNT = 3  # 單日日誌保留滾動備份份數
CLI_MAX_AUTH_RETRIES = 3  # CLI 金鑰密碼解鎖重試次數上限
DEFAULT_MAX_PARALLEL_SUBAGENTS = 3  # 平行子代理批次並發執行上限（防範觸發供應商 429 限流）
DEFAULT_MAX_BACKGROUND_SUBAGENTS = 3  # 同時執行中之背景唯讀子代理數量上限
DEFAULT_MAX_BACKGROUND_JOB_HISTORY = 20  # 記憶體保留之已完成／失敗／取消背景工作紀錄上限
DEFAULT_BG_CHECK_MAX_WAIT_SEC = 30  # check_background_subagent 單次最長阻塞等待秒數

# Housekeeping 與日誌／暫存／備份生命週期管理常數
DEFAULT_HOUSEKEEPING_TTL_DAYS = 7  # 暫存輸出、剪貼簿截圖、研究檢查點與孤兒備份預設保留天數
DEFAULT_HOUSEKEEPING_LOG_TTL_MULTIPLIER = 2  # 系統日誌 TTL 倍率（預設 14 天）
DEFAULT_HOUSEKEEPING_ACTIVE_BACKUP_TTL_MULTIPLIER = 4  # 時光機活躍備份 TTL 倍率（預設 28 天）
DEFAULT_HOUSEKEEPING_MAX_FILES = 100  # 單一暫存目錄允許保留的最大檔案數
DEFAULT_HOUSEKEEPING_MAX_SIZE_MB = 50  # 單一暫存目錄容量配額上限 (MB)
DEFAULT_HOUSEKEEPING_BACKUP_MAX_SIZE_MB = 200  # 時光機備份目錄總容量配額上限 (MB)
DEFAULT_HOUSEKEEPING_COOLDOWN_SECONDS = 86400  # 自動 Housekeeping 冷卻期 (24 小時)
DEFAULT_SESSION_PRUNE_DAYS = 30  # /prune 手動清理歷史對話預設天數門檻
DEFAULT_SESSION_PRUNE_KEEP = 20  # /prune 手動清理歷史對話預設保留最新筆數


# ==============================================================================
# 7. AI 模型供應商識別常數 (Model Provider Identifiers)
# ==============================================================================
# 設計意圖：
# 集中管理各 LLM 供應商的唯一標識字串，供 EngineFactory、組態設定與路由分派使用。
# ==============================================================================
PROVIDER_AZURE = "azure"
PROVIDER_CEREBRAS = "cerebras"
PROVIDER_GEMINI = "gemini"
PROVIDER_GEMINI_OAUTH = "gemini_oauth"
PROVIDER_GROQ = "groq"
PROVIDER_MISTRAL = "mistral"
PROVIDER_OLLAMA = "ollama"
PROVIDER_OLLAMACLOUD = "ollamacloud"
PROVIDER_OPENAI = "openai"
PROVIDER_OPENAI_API = "openai-api"
PROVIDER_OPENAI_COMPATIBLE = "openai-compatible"
PROVIDER_OPENROUTER = "openrouter"
PROVIDER_SAMBANOVA = "sambanova"


# ==============================================================================
# 8. OAuth 端點與 URL 常數 (OAuth Endpoints & URLs)
# ==============================================================================
# 設計意圖：
# 集中管理各 OAuth 供應商之授權與 Token 換發/重新整理端點 URL。
# ==============================================================================
DEFAULT_OPENAI_AUTH_URL = "https://auth.openai.com/oauth/authorize"
DEFAULT_OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_GEMINI_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_GEMINI_TOKEN_URL = "https://oauth2.googleapis.com/token"

OAUTH_TOKEN_URLS: dict[str, str] = {
    PROVIDER_OPENAI: DEFAULT_OPENAI_TOKEN_URL,
    PROVIDER_GEMINI_OAUTH: DEFAULT_GEMINI_TOKEN_URL,
    "gemini-oauth": DEFAULT_GEMINI_TOKEN_URL,
}

OAUTH_AUTH_URLS: dict[str, str] = {
    PROVIDER_OPENAI: DEFAULT_OPENAI_AUTH_URL,
    PROVIDER_GEMINI_OAUTH: DEFAULT_GEMINI_AUTH_URL,
    "gemini-oauth": DEFAULT_GEMINI_AUTH_URL,
}


# ==============================================================================
# 9. 迴圈防發散、壓縮紀錄與子行程隔離常數 (Loop Guard, Ledger & Env Scrubbing)
# ==============================================================================
# 設計意圖：
# 集中管理重複工具呼叫偵測門檻、壓縮操作紀錄字元上限，以及子行程環境變數剝除清單。
# ==============================================================================
DIVERGENCE_WARN_REPEAT_COUNT = 2  # 連續相同工具與參數呼叫達此次數時注入收斂提醒
DIVERGENCE_STOP_REPEAT_COUNT = 3  # 連續相同工具與參數呼叫達此次數時主動中止迴圈
COMPACTION_LEDGER_MAX_CHARS = 2400  # 壓縮時結構化操作紀錄之最大字元上限

SENSITIVE_ENV_EXACT_KEYS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "SAMBANOVA_API_KEY",
    "CEREBRAS_API_KEY",
    "OLLAMA_API_KEY",
    "OLLAMA_CLOUD_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "POCKET_AGENT_MASTER_PASSWORD",
)

SENSITIVE_ENV_PREFIXES: tuple[str, ...] = (
    "POCKET_AGENT_OAUTH_",
    "POCKET_AGENT_TOKEN_",
)

SECRET_PLACEHOLDER_KEYRING = "USE_KEYRING"  # 憑證存於作業系統原生 Keyring 之佔位符號
SECRET_PLACEHOLDER_ENCRYPTED = "USE_ENCRYPTED"  # 憑證存於主密碼 JWE 加密庫之佔位符號
SECRET_PLACEHOLDERS: tuple[str, ...] = (
    SECRET_PLACEHOLDER_KEYRING,
    SECRET_PLACEHOLDER_ENCRYPTED,
)


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
    operation_drain_timeout_sec: float = DEFAULT_OPERATION_DRAIN_TIMEOUT_SEC
    cli_timeout_sec: int = DEFAULT_CLI_TIMEOUT_SEC
    max_cli_timeout_sec: int = MAX_CLI_TIMEOUT_SEC
