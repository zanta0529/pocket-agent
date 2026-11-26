# GitHub PR／Issue 與 CI 整合 MCP 伺服器（`github`）

本套件為 PocketAgent 隨附之 GitHub REST API 整合 MCP 伺服器，提供 Pull Request／Issue 檢索與更新、行內 Code Review 讀取與提交、CI Check／GitHub Actions 診斷、儲存庫唯讀探索與三階段範本感知 PR 建立功能，並支援企業自架 GitHub Enterprise Server（GHES）端點與雙向機密防護。

---

## 模組架構

遵循「共用基礎設施（`_shared/`）＋ 領域子套件（`github/`）＋ 頂層薄入口（`github_mcp.py`）」三層設計：

| 路徑 | 職責說明 |
| :--- | :--- |
| `mcp_servers/github_mcp.py` | Stdio MCP 薄入口，維護啟動契約與對外匯出清單 |
| `mcp_servers/github/server.py` | 透過 `create_mcp_server("github", ..., lifespan=server_lifespan)` 組裝並註冊工具群組 |
| `mcp_servers/github/client.py` | 非同步 `httpx.AsyncClient` 連線池、`truststore` 系統憑證庫、手動 `3xx` 重導向驗證、2 MB 串流上限、Token 解析與集中稽核 |
| `mcp_servers/github/security.py` | 儲存庫白名單、路徑防穿越與百分位編碼、搜尋範圍鎖定、重導向驗證與雙向機密防護 |
| `mcp_servers/github/templates.py` | 三階段 PR 範本載入（本機 ➔ 遠端 ➔ 內建）與預設目標分支解析 |
| `mcp_servers/github/helpers.py` | 跨模組共用之 GitHub API 資料萃取與格式化輔助 |
| `mcp_servers/github/tools/pull_requests.py` | PR 檢索、Diff、審查意見與行內留言、範本上下文、PR 建立／更新與審查提交工具 |
| `mcp_servers/github/tools/checks.py` | CI Check Runs、Annotations、Workflow Runs 與 Jobs／Steps 診斷工具 |
| `mcp_servers/github/tools/issues.py` | Issue 與討論串讀取、篩選、搜尋、建立與更新工具 |
| `mcp_servers/github/tools/repository.py` | 儲存庫中繼資料、分支、Tag、Commit、檔案／目錄內容、Release 與 Labels 唯讀探索工具 |

---

## 快速開始與 `mcp_config.yaml` 設定

在專案設定檔 `mcp_config.yaml` 的 `servers:` 區段加入以下設定（21 項唯讀工具建議列入 `safe_read_tools`；6 項寫入工具**不應列入**以強制保留互動預覽確認）：

```yaml
servers:
  github:
    transport: stdio
    command: python
    args: ["mcp_servers/github_mcp.py"]
    # is_sensitive: true                  # 選填：存取機密私有儲存庫時可設為 true，強制對話紀錄零落盤
    safe_read_tools:
      - github_list_prs
      - github_get_pr
      - github_get_pr_files
      - github_get_pr_reviews
      - github_get_pr_review_comments
      - github_prepare_pr_context
      - github_get_pr_checks
      - github_get_check_annotations
      - github_list_workflow_runs
      - github_get_workflow_run_jobs
      - github_read_issue
      - github_read_comments
      - github_list_issues
      - github_search_issues
      - github_get_repo
      - github_list_branches
      - github_list_tags
      - github_list_commits
      - github_get_file_content
      - github_list_releases
      - github_list_labels
    env:
      GITHUB_TOKEN: ""                    # 留空將於執行時安全輸入，並可保存至 Keyring／JWE（寫回 USE_KEYRING / USE_ENCRYPTED）
      GITHUB_ALLOWED_REPOS: ""            # 選填：允許存取之 owner/repo 白名單（逗號分隔，如 "org/repo1,org/repo2"）
      GITHUB_API_BASE_URL: ""             # 選填：GHES API 基底網址（須為 HTTPS，如 "https://github.corp.com/api/v3"）
```

### 環境變數說明

| 參數名稱 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `GITHUB_TOKEN` | `""` | GitHub Personal Access Token；建議留空或設為 `"USE_KEYRING"`／`"USE_ENCRYPTED"`。留空時將於首次呼叫工具於 CLI 以隱藏輸入詢問，並可選擇保存至系統 Keyring／JWE 加密庫；清空佔位符號將於下次執行重新詢問 |
| `GITHUB_ALLOWED_REPOS` | `""` | 允許存取之儲存庫白名單（逗號分隔 `owner/repo`，不分大小寫）；留空時允許存取該 Token 授權範圍內之儲存庫 |
| `GITHUB_API_BASE_URL` | `""` | 企業自架 GitHub Enterprise Server（GHES）之 API 基底網址（須為 HTTPS 且不可含查詢參數或使用者憑證）；留空時使用官方 `https://api.github.com` |

啟動 PocketAgent 後，可在 CLI 輸入 `/mcp` 確認 `github` 已掛載 27 個工具，或輸入 `/mcp github_prepare_pr_context` 檢視單一工具說明。

---

## 工具清單速查（共 27 項：21 項唯讀 ＋ 6 項強制確認寫入）

### 1. Pull Request 檢索與上下文準備（唯讀）
| 工具名稱 | 用途說明 | 主要參數 |
| :--- | :--- | :--- |
| `github_list_prs` | 列出指定儲存庫的 Pull Requests（含 Draft 與 `head -> base` 分支資訊） | `owner`、`repo`、`state`、`head`、`base`、`sort`、`direction`、`per_page`、`page` |
| `github_get_pr` | 取得單一 PR 完整詳情（含 `head.sha`、分支方向、合併狀態、待審查者與變更統計） | `owner`、`repo`、`pull_number` |
| `github_get_pr_files` | 取得指定 PR 的變更檔案清單、行數統計與各檔 Diff Patch | `owner`、`repo`、`pull_number`、`per_page`、`page`、`include_patch` |
| `github_get_pr_reviews` | 取得指定 PR 的 Code Review 審查紀錄（`APPROVED`、`CHANGES_REQUESTED` 等）與審查意見 | `owner`、`repo`、`pull_number`、`per_page`、`page` |
| `github_get_pr_review_comments` | 讀取指定 PR 掛載於程式碼檔案與行號上的行內審查留言（含 `diff_hunk` 與回覆串） | `owner`、`repo`、`pull_number`、`per_page`、`page` |
| `github_prepare_pr_context` | 依「本機範本 ➔ 遠端範本（含大小寫與 `docs/`） ➔ 內建標準範本」取得 PR 範本，並比對 `base...head` 變更摘要 | `owner`、`repo`、`head`、`base` |

### 2. CI Check 與 GitHub Actions 診斷（唯讀）
| 工具名稱 | 用途說明 | 主要參數 |
| :--- | :--- | :--- |
| `github_get_pr_checks` | 查詢指定 Commit SHA、分支或標籤的 GitHub Actions／CI Check Runs 狀態（含 `check_run_id` 與摘要） | `owner`、`repo`、`ref`、`per_page`、`page` |
| `github_get_check_annotations` | 取得指定 Check Run 的詳細錯誤／警告註記（檔案、行號與訊息） | `owner`、`repo`、`check_run_id`、`per_page`、`page` |
| `github_list_workflow_runs` | 列出指定儲存庫的 GitHub Actions Workflow 執行紀錄 | `owner`、`repo`、`branch`、`status`、`event`、`per_page`、`page` |
| `github_get_workflow_run_jobs` | 查詢指定 Workflow Run 內的 Jobs 與 Steps 執行狀態 | `owner`、`repo`、`run_id`、`filter_mode`、`per_page`、`page` |

### 3. Issue 與討論串檢索（唯讀）
| 工具名稱 | 用途說明 | 主要參數 |
| :--- | :--- | :--- |
| `github_read_issue` | 讀取指定 Issue 或 PR 的標題、作者、狀態、標籤、指派人、里程碑、留言數與內文 | `owner`、`repo`、`issue_number` |
| `github_read_comments` | 讀取指定 Issue 或 PR 的留言討論串（支援分頁） | `owner`、`repo`、`issue_number`、`per_page`、`page` |
| `github_list_issues` | 列出指定儲存庫的 Issues（支援狀態、標籤、指派人、建立者、里程碑與排序篩選，預設排除 PR） | `owner`、`repo`、`state`、`labels`、`assignee`、`creator`、`milestone`、`sort`、`direction`、`per_page`、`page`、`include_prs` |
| `github_search_issues` | 在指定儲存庫內以關鍵字搜尋 Issues 與 PR（強制鎖定 `repo:owner/repo` 範圍） | `owner`、`repo`、`query`、`state`、`per_page`、`page` |

### 4. 儲存庫唯讀探索（唯讀）
| 工具名稱 | 用途說明 | 主要參數 |
| :--- | :--- | :--- |
| `github_get_repo` | 查詢指定儲存庫的預設分支、可見性、主要語言、Topics 與基本統計 | `owner`、`repo` |
| `github_list_branches` | 列出指定儲存庫的遠端分支清單（可篩選僅受保護分支） | `owner`、`repo`、`protected_only`、`per_page`、`page` |
| `github_list_tags` | 列出指定儲存庫的 Git Tags 清單 | `owner`、`repo`、`per_page`、`page` |
| `github_list_commits` | 列出指定儲存庫的 Commit 歷史紀錄（支援分支／SHA、路徑與作者篩選） | `owner`、`repo`、`sha`、`path`、`author`、`per_page`、`page` |
| `github_get_file_content` | 唯讀取得指定儲存庫與 `ref` 下的單一檔案內容或目錄清單（含二進位偵測與大小限制） | `owner`、`repo`、`path`、`ref` |
| `github_list_releases` | 列出指定儲存庫的 Releases 發行版本清單與摘要 | `owner`、`repo`、`per_page`、`page` |
| `github_list_labels` | 列出指定儲存庫目前可用的標籤（Labels）與說明 | `owner`、`repo`、`per_page`、`page` |

### 5. 寫入與協作操作（強制預覽／人工確認，不列入 `safe_read_tools`）
| 工具名稱 | 用途說明 | 主要參數 |
| :--- | :--- | :--- |
| `github_create_pr` | 建立 Pull Request（CLI 強制彈出完整動作、分支方向、標題與內文預覽，絕不自動 Merge） | `owner`、`repo`、`title`、`head`、`base`、`body`、`draft`、`labels`、`reviewers` |
| `github_update_pr` | 更新既有 PR 的標題、內文、狀態（`open`/`closed`）或目標分支（`base`） | `owner`、`repo`、`pull_number`、`title`、`body`、`state`、`base` |
| `github_create_pr_review` | 提交 PR 審查意見與行內留言（僅允許 `COMMENT` 或 `REQUEST_CHANGES`，**禁止 `APPROVE`**） | `owner`、`repo`、`pull_number`、`event`、`body`、`comments`、`commit_id` |
| `github_create_issue` | 在指定儲存庫建立新的 Issue（外發前自動掃描機密） | `owner`、`repo`、`title`、`body`、`labels`、`assignees`、`milestone` |
| `github_update_issue` | 更新既有 Issue 的標題、內文、狀態、關閉原因、標籤、指派人或里程碑 | `owner`、`repo`、`issue_number`、`title`、`body`、`state`、`state_reason`、`labels`、`assignees`、`milestone` |
| `github_create_comment` | 在指定的 Issue 或 Pull Request 新增留言 | `owner`、`repo`、`issue_number`、`body` |

---

## 實務開發與協作場景與建議提問範例

| 協作情境 | 對應工具 | 建議對 PocketAgent 提問方式 |
| :--- | :--- | :--- |
| **範本感知建立 PR** | `github_prepare_pr_context` ＋ `github_create_pr` | 「幫我準備 `org/repo` 從 `feat/auth` 到 `main` 的 PR 範本與變更摘要，草擬標題與內文後建立 Draft PR。」 |
| **深度 Code Review 與行內審查** | `github_get_pr` ＋ `github_get_pr_files` ＋ `github_create_pr_review` | 「檢視 `org/repo` PR #128 的變更檔案與 Diff，整理潛在風險並以 `COMMENT` 提交行內審查意見。」 |
| **排查 CI／Actions 失敗根因** | `github_get_pr_checks` ＋ `github_get_check_annotations` ＋ `github_list_workflow_runs` ＋ `github_get_workflow_run_jobs` | 「查一下 PR #128 失敗的 CI Check 與 Annotations，並對照最近一次 Workflow Run 的失敗 Step。」 |
| **Issue 分類與重複問題排查** | `github_list_issues` ＋ `github_search_issues` ＋ `github_read_issue` ＋ `github_read_comments` | 「在 `org/repo` 搜尋和 `timeout` 相關的歷史 Issue 與討論串，確認是否為已知問題。」 |
| **跨分支變更追查與遠端讀檔** | `github_list_commits` ＋ `github_get_file_content` ＋ `github_list_branches` | 「列出 `org/repo` 在 `release/v2` 分支近 10 筆針對 `src/` 的 Commit，並讀取該分支的 `pyproject.toml`。」 |
| **Release 整理與標籤核對** | `github_list_releases` ＋ `github_list_tags` | 「比對 `org/repo` 最近的 Git Tags 與正式 Releases，整理兩者之間的差異。」 |
| **建立／更新 Issue 與回覆討論** | `github_create_issue` ＋ `github_update_issue` ＋ `github_create_comment` | 「幫我把這個錯誤整理成 Issue（加上 `bug` 標籤），或在 Issue #42 補充我們的重現步驟。」 |

---

## 建議協作工作流與安全邊界

1. **先準備範本與總覽、再深入抓 Diff**：建立 PR 前先呼叫 `github_prepare_pr_context` 對齊儲存庫範本與預設主分支；進行 PR 審查或 CI 排錯時，建議先呼叫 `github_get_pr`／`github_get_pr_checks` 取得總覽，再針對關鍵檔案或失敗的 `check_run_id` 呼叫 `github_get_pr_files`（必要時先設 `include_patch=False`）與 `github_get_check_annotations`，可有效節省 Token 與 API 配額。
2. **人工核准把關（No Auto-Merge / No Auto-Approve）**：`github_create_pr`、`github_update_pr` 與 `github_create_pr_review` 受 `ConfirmationBridge` 強制預覽確認且不適用自動放行；`github_create_pr_review` 明確封鎖 `APPROVE`，且伺服器不提供任何 Merge 或刪除工具，確保最終核准與合併永遠由人工於 GitHub 網站完成。
3. **雙向機密防護與端點安全**：所有外發寫入工具送出前自動掃描私鑰、雲端／AI Token、內嵌帳密 URL 與敏感鍵值對並立即阻斷外洩；所有 API 回應受 `2 MB` 串流上限與 `12,000` 字元預裁切遮罩保護。支援 `GITHUB_ALLOWED_REPOS` 白名單、HTTP `3xx` 重導向跨網域／非 HTTPS 攔截、`GITHUB_TOKEN` Keyring／JWE 零明碼儲存，以及 `agent.log` 的 `[AUDIT] [MCP:github]` 集中稽核紀錄。
