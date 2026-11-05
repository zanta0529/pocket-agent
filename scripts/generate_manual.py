import os
import re


def read_md(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


def demote_headers(text, offset):
    """將 Markdown 標題層級往下降（增加指定數量的 #）"""
    lines = text.split("\n")
    out = []
    for line in lines:
        match = re.match(r"^(#+)(.*)", line)
        if match:
            out.append("#" * offset + match.group(1) + match.group(2))
        else:
            out.append(line)
    return "\n".join(out)


def strip_title(text):
    """移除檔案最開頭的單行最高層級標題 (例如 '# Title')"""
    return re.sub(r"^\s*#\s+[^\n]+\n+", "", text)


def build_manual():
    # 因為腳本放在 scripts/ 下，所以根目錄是上一層
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    docs_dir = os.path.join(base_dir, "docs")
    os.makedirs(docs_dir, exist_ok=True)

    readme_path = os.path.join(base_dir, "README.md")
    readme = read_md(readme_path)

    files = {
        "config_spec": read_md(os.path.join(docs_dir, "configuration-specification.md")),
        "agent_config": read_md(os.path.join(docs_dir, "agent-configuration-standards.md")),
        "hooks": read_md(os.path.join(docs_dir, "hooks-lifecycle.md")),
        "llm_adapter": read_md(os.path.join(docs_dir, "llm-adapter-architecture.md")),
        "mcp": read_md(os.path.join(docs_dir, "mcp-architecture.md")),
        "oauth": read_md(os.path.join(docs_dir, "oauth-mechanism.md")),
        "tool_subagent": read_md(os.path.join(docs_dir, "tool-subagent-mechanism.md")),
        "cli_limits": read_md(os.path.join(docs_dir, "cli-usage-limitations.md")),
        "skills_comp": read_md(os.path.join(docs_dir, "subagents-skills-comparison.md")),
        "bm25": read_md(os.path.join(docs_dir, "bm25-visual-explanation.md")),
        "arch_report": read_md(os.path.join(docs_dir, "agent-workspace-architecture-report.md")),
        "routing": read_md(os.path.join(docs_dir, "three-tier-routing-architecture.md")),
        "crypto": read_md(os.path.join(docs_dir, "sensitive-data-protection.md")),
    }

    def extract_section(start_pattern, end_pattern=None):
        start = re.search(start_pattern, readme)
        if not start:
            return ""
        start_idx = start.start()
        if end_pattern:
            end = re.search(end_pattern, readme[start_idx:])
            if end:
                return readme[start_idx : start_idx + end.start()].strip()
        return readme[start_idx:].strip()

    # 解析 README 各大區塊並去除區塊原本的標題
    readme_intro = extract_section(r"# PocketAgent 🤖", r"## 🚀 初次安裝與快速啟動")
    readme_intro = re.sub(r"^\s*# PocketAgent 🤖\n+", "", readme_intro)
    readme_intro = re.sub(r"### 📑 目錄.*", "", readme_intro, flags=re.DOTALL).strip()

    readme_install = extract_section(r"## 🚀 初次安裝與快速啟動", r"## 💻 快速上手與日常操作")
    readme_install = re.sub(r"^## 🚀 初次安裝與快速啟動.*?\n", "", readme_install).strip()

    readme_usage = extract_section(r"## 💻 快速上手與日常操作", r"## ✨ 核心架構與特色")
    readme_usage = re.sub(r"^## 💻 快速上手與日常操作.*?\n", "", readme_usage).strip()

    readme_features = extract_section(r"## ✨ 核心架構與特色", r"## ⚙️ 進階設定與客製化")
    readme_features = re.sub(r"^## ✨ 核心架構與特色.*?\n", "", readme_features).strip()

    readme_config = extract_section(r"## ⚙️ 進階設定與客製化", r"## 🛠️ 開發與貢獻")
    readme_config = re.sub(r"^## ⚙️ 進階設定與客製化.*?\n", "", readme_config).strip()

    readme_build = extract_section(r"## 📦 發布與建置", r"## ⚠️ 免責聲明")
    readme_build = re.sub(r"^## 📦 發布與建置.*?\n", "", readme_build).strip()

    readme_disclaimer = extract_section(r"## ⚠️ 免責聲明", r"## 📜 授權條款")
    readme_disclaimer = re.sub(r"^## ⚠️ 免責聲明.*?\n", "", readme_disclaimer).strip()
    readme_disclaimer = re.sub(r"\n---\s*$", "", readme_disclaimer).strip()

    manual = []
    manual.append("<!--")
    manual.append("⚠️ 警告：本檔案由 scripts/generate_manual.py 自動生成，請勿直接編輯！")
    manual.append("如果您要修改內容，請前往 `README.md` 或 `docs/` 目錄下修改對應的原始檔案。")
    manual.append("任何對此檔案的直接修改都會在下次打包時被覆蓋。")
    manual.append("-->\n")
    manual.append("# PocketAgent 完整操作說明書\n")
    manual.append(
        "> 本文件整合了專案的各項指南與架構說明，"
        "依據「安裝 → 日常操作 → 設定與擴充 → 架構與安全 → 疑難排解」重新編排，方便您一站式查閱。\n"
    )

    manual.append("## 目錄\n")
    manual.append("- [一、安裝 (Installation)](#一安裝-installation)")
    manual.append("  - [1.1 環境需求與啟動](#11-環境需求與啟動)")
    manual.append("  - [1.2 發布與建置](#12-發布與建置)")
    manual.append("- [二、日常操作 (Daily Operations)](#二日常操作-daily-operations)")
    manual.append("  - [2.1 基本操作與指令](#21-基本操作與指令)")
    manual.append("  - [2.2 Subagents 與 Skills 的差異與使用](#22-subagents-與-skills-的差異與使用)")
    manual.append("- [三、設定與擴充 (Configuration & Extensions)](#三設定與擴充-configuration--extensions)")
    manual.append("  - [3.1 進階設定與 MCP](#31-進階設定與-mcp)")
    manual.append(
        "  - [3.2 設定檔規格說明書 (Configuration Specification)](#32-設定檔規格說明書-configuration-specification)"
    )
    manual.append(
        "  - [3.3 代理配置標準 (Agent Configuration Standards)](#33-代理配置標準-agent-configuration-standards)"
    )
    manual.append("  - [3.4 Hooks 生命週期機制](#34-hooks-生命週期機制)")
    manual.append("- [四、架構與安全 (Architecture & Security)](#四架構與安全-architecture--security)")
    manual.append("  - [4.1 雙軌大模型轉接架構](#41-雙軌大模型轉接架構)")
    manual.append("  - [4.2 OAuth 2.0 認證機制](#42-oauth-20-認證機制)")
    manual.append("  - [4.3 MCP 伺服器掛載與注入機制](#43-mcp-伺服器掛載與注入機制)")
    manual.append("  - [4.4 工具呼叫與 Subagent 委派機制](#44-工具呼叫與-subagent-委派機制)")
    manual.append("  - [4.5 BM25 向量檢索與記憶機制](#45-bm25-向量檢索與記憶機制)")
    manual.append("  - [4.6 敏感設定加密與 OS Keyring 整合](#46-敏感設定加密與-os-keyring-整合)")
    manual.append("  - [4.7 子代理權限與工作區隔離機制](#47-子代理權限與工作區隔離機制)")
    manual.append("  - [4.8 三層式路由架構](#48-三層式路由架構)")
    manual.append("- [五、疑難排解 (Troubleshooting)](#五疑難排解-troubleshooting)")
    manual.append("  - [5.1 CLI 操作與限制說明](#51-cli-操作與限制說明)")
    manual.append("- [六、免責聲明 (Disclaimer)](#六免責聲明-disclaimer)\n")

    manual.append("---\n")
    manual.append(readme_intro + "\n")

    manual.append("## 一、安裝 (Installation)\n")
    manual.append("### 1.1 環境需求與啟動\n")
    manual.append(demote_headers(readme_install, 1) + "\n")

    manual.append("### 1.2 發布與建置\n")
    manual.append(demote_headers(readme_build, 1) + "\n")

    manual.append("## 二、日常操作 (Daily Operations)\n")
    manual.append("### 2.1 基本操作與指令\n")
    manual.append(demote_headers(readme_usage, 1) + "\n")

    manual.append("### 2.2 Subagents 與 Skills 的差異與使用\n")
    manual.append(demote_headers(strip_title(files["skills_comp"]), 2) + "\n")

    manual.append("## 三、設定與擴充 (Configuration & Extensions)\n")
    manual.append("### 3.1 進階設定與 MCP\n")
    manual.append(demote_headers(readme_config, 1) + "\n")

    manual.append("### 3.2 設定檔規格說明書 (Configuration Specification)\n")
    manual.append(demote_headers(strip_title(files["config_spec"]), 2) + "\n")

    manual.append("### 3.3 代理配置標準 (Agent Configuration Standards)\n")
    manual.append(demote_headers(strip_title(files["agent_config"]), 2) + "\n")

    manual.append("### 3.4 Hooks 生命週期機制\n")
    manual.append(demote_headers(strip_title(files["hooks"]), 2) + "\n")

    manual.append("## 四、架構與安全 (Architecture & Security)\n")
    manual.append("### 4.1 雙軌大模型轉接架構\n")
    manual.append(demote_headers(strip_title(files["llm_adapter"]), 2) + "\n")

    manual.append("### 4.2 OAuth 2.0 認證機制\n")
    manual.append(demote_headers(strip_title(files["oauth"]), 2) + "\n")

    manual.append("### 4.3 MCP 伺服器掛載與注入機制\n")
    manual.append(demote_headers(strip_title(files["mcp"]), 2) + "\n")

    manual.append("### 4.4 工具呼叫與 Subagent 委派機制\n")
    manual.append(demote_headers(strip_title(files["tool_subagent"]), 2) + "\n")

    manual.append("### 4.5 BM25 向量檢索與記憶機制\n")
    manual.append(demote_headers(strip_title(files["bm25"]), 2) + "\n")

    manual.append("### 4.6 敏感設定加密與 OS Keyring 整合\n")
    manual.append(demote_headers(strip_title(files["crypto"]), 2) + "\n")

    manual.append("### 4.7 子代理權限與工作區隔離機制\n")
    manual.append(demote_headers(strip_title(files["arch_report"]), 2) + "\n")

    manual.append("### 4.8 三層式路由架構\n")
    manual.append(demote_headers(strip_title(files["routing"]), 2) + "\n")

    manual.append("## 五、疑難排解 (Troubleshooting)\n")
    manual.append("### 5.1 CLI 操作與限制說明\n")
    manual.append(demote_headers(strip_title(files["cli_limits"]), 2) + "\n")

    if readme_disclaimer:
        manual.append("## 六、免責聲明 (Disclaimer)\n")
        manual.append(readme_disclaimer + "\n")

    dist_dir = os.path.join(base_dir, "dist")
    os.makedirs(dist_dir, exist_ok=True)
    output_path = os.path.join(dist_dir, "PocketAgent_Manual.md")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(manual))


if __name__ == "__main__":
    build_manual()
    print("說明書腳本執行完畢，已成功生成至 dist/PocketAgent_Manual.md")
