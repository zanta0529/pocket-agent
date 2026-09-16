"""Skill 系統：markdown 知識包，模型判斷相關時自行載入。

Skill 是「操作手冊」而非可執行工具——一份 SKILL.md 教模型
怎麼做某類任務（報告格式、SOP、公司術語等）。

目錄結構（兩處都會掃描，同名時使用者目錄優先）：
    <bundle>/skills/<name>/SKILL.md        # 內建，隨程式發佈（唯讀）
    ~/.pocket-agent/skills/<name>/SKILL.md     # 使用者自訂／匯入（可寫）

SKILL.md 格式：
    ---
    name: weekly-report
    description: 一句話說明何時該用這個 skill
    ---
    （給模型的完整指示，markdown 自由發揮）
"""

import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

from pocket_agent.system.paths import bundle_dir, user_dir
from pocket_agent.system.registry import MarkdownRegistry, parse_frontmatter

log = logging.getLogger("pocket-agent")

REPO_SKILLS_DIR = bundle_dir() / "skills"  # 內建，隨程式發佈（唯讀）
USER_SKILLS_DIR = user_dir() / "skills"  # 使用者自訂／匯入（可寫）


class SkillRegistry(MarkdownRegistry):
    def get_glob_pattern(self) -> str:
        return "*/SKILL.md"

    def parse_item(self, path: Path, meta: dict, body: str) -> dict:
        return {
            "name": meta.get("name") or path.parent.name,
            "description": meta.get("description", ""),
            "path": path,
        }


# 全域實例
_registry = SkillRegistry(REPO_SKILLS_DIR, USER_SKILLS_DIR)


# ── 掃描與載入 ────────────────────────────────────


def _get_workspace_skill_dirs(workspace_dirs: list[str] | None) -> list[Path]:
    dirs = [Path.cwd() / ".agents" / "skills"]
    if workspace_dirs:
        for wd in workspace_dirs:
            p = Path(wd) / ".agents" / "skills"
            if p not in dirs:
                dirs.append(p)
    return dirs


def list_skills(workspace_dirs: list[str] | None = None) -> list[dict]:
    """列出所有 skill 的 {name, description, path}，專案目錄與使用者目錄優先。"""
    extra_dirs = _get_workspace_skill_dirs(workspace_dirs)
    return _registry.list_all(extra_dirs=extra_dirs)


def load_skill(name: str, workspace_dirs: list[str] | None = None) -> str | None:
    """回傳指定 skill 的本文；找不到時回傳 None。"""
    extra_dirs = _get_workspace_skill_dirs(workspace_dirs)
    return _registry.read_body(name, extra_dirs=extra_dirs)


# ── ZIP 匯入（相容 ChatGPT / Claude 的 Agent Skills zip）──


def import_skill_zip(zip_path: str | Path, overwrite: bool = False) -> list[str]:
    """匯入 Agent Skills 格式的 zip，回傳匯入的 skill 名稱列表。

    支援兩種常見結構：
      1. <skill-name>/SKILL.md （ChatGPT 官方匯出：單一頂層資料夾）
      2. SKILL.md 在 zip 根目錄（資料夾名取 frontmatter name 或 zip 檔名）
    一個 zip 含多個 skill 資料夾也可以。

    衝突時 raise FileExistsError（訊息含衝突名稱）；
    格式錯誤 raise ValueError。
    """
    zip_path = Path(zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        # zip-slip 防護：拒絕絕對路徑與 ".."
        for member in zf.namelist():
            part_list = Path(member).parts
            if Path(member).is_absolute() or ".." in part_list:
                raise ValueError(f"zip 內含不安全路徑：{member}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            zf.extractall(tmp_dir)
            tmp = Path(tmp_dir)

            if (tmp / "SKILL.md").exists():
                # SKILL.md 直接在根目錄 → 整包視為一個 skill
                skill_dirs = [tmp]
            else:
                # 只取「最上層」含 SKILL.md 的資料夾（略過巢狀誤判）
                candidates = sorted(
                    (md.parent for md in tmp.rglob("SKILL.md")),
                    key=lambda p: len(p.parts),
                )
                skill_dirs = []
                for d in candidates:
                    if not any(a in d.parents for a in skill_dirs):
                        skill_dirs.append(d)

            if not skill_dirs:
                raise ValueError("zip 裡找不到 SKILL.md，不是有效的 skill 包。")

            # 先算目的地並檢查衝突
            plans: list[tuple[Path, Path]] = []
            for d in skill_dirs:
                if d == tmp:
                    meta, _ = parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
                    name = meta.get("name") or zip_path.stem
                else:
                    name = d.name
                # 匯入一律進使用者目錄：內建目錄打包後唯讀，
                # 且會被下次程式更新覆蓋掉。
                plans.append((d, USER_SKILLS_DIR / name))

            conflicts = [t.name for _s, t in plans if t.exists()]
            if conflicts and not overwrite:
                raise FileExistsError("、".join(conflicts))

            imported = []
            for source, target in plans:
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(source, target)
                imported.append(target.name)
                log.info("skill 已匯入：%s ← %s", target.name, zip_path)

            sync_index()
            return imported


# ── 寫入與儲存 ────────────────────────────────────


def save_skill(name: str, description: str, body: str, scope: str = "global") -> Path:
    """寫入 Skill 至指定範圍，回傳儲存的路徑。
    scope: "global" (存入 ~/.pocket-agent/skills) 或 "workspace" (存入 cwd/.agents/skills)
    """
    if scope == "workspace":
        base_dir = Path.cwd() / ".agents" / "skills"
    else:
        base_dir = USER_SKILLS_DIR

    skill_dir = base_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_path = skill_dir / "SKILL.md"

    # 建立包含 frontmatter 的完整內容
    content = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"
    skill_path.write_text(content, encoding="utf-8")

    log.info("skill 已儲存：%s ← %s", name, skill_path)

    # 同步 FTS5 索引 (暫不傳 workspace_dirs，下次搜尋時會再次補齊)
    sync_index()

    return skill_path


# ── BM25 技能檢索 (SQLite FTS5) ──────────────────────

_last_sync_state = None


def _get_skills_state(workspace_dirs: list[str] | None = None) -> int:
    """透過所有 SKILL.md 的路徑與最後修改時間 (mtime) 產生狀態指紋。"""
    skills = list_skills(workspace_dirs)
    state_items = []
    for s in skills:
        p = s["path"]
        if p.exists():
            state_items.append((str(p), p.stat().st_mtime))
    return hash(tuple(sorted(state_items)))


def _get_db_path() -> Path:
    db_dir = user_dir() / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "skills_index.db"


def sync_index(workspace_dirs: list[str] | None = None):
    """將目前的 skill 同步到 SQLite FTS5 虛擬資料表中。"""
    global _last_sync_state
    import sqlite3

    db_path = _get_db_path()

    # 策略 3: 直接砍檔重建 (Disposable Cache)
    # 確保 100% 沒有空位碎片與幽靈資料殘留
    if db_path.exists():
        try:
            db_path.unlink()
        except OSError as e:
            log.warning("無法刪除舊的 skills_index.db，將退回 SQL 刪除模式: %s", e)

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(name, description, body)")
        conn.execute("DELETE FROM skills_fts")  # 防禦性寫法，萬一 unlink 失敗仍可清空資料
        skills = list_skills(workspace_dirs)
        for s in skills:
            body = load_skill(s["name"], workspace_dirs)
            if body:
                conn.execute(
                    "INSERT INTO skills_fts (name, description, body) VALUES (?, ?, ?)",
                    (s["name"], s["description"], body),
                )
        conn.commit()
    # 更新指紋，避免重複同步
    _last_sync_state = _get_skills_state(workspace_dirs)


def sync_index_if_needed(workspace_dirs: list[str] | None = None):
    """如果技能檔案有異動 (mtime 改變或檔案增減)，或 DB 不存在時，才執行同步。"""
    global _last_sync_state
    current_state = _get_skills_state(workspace_dirs)
    if _last_sync_state == current_state and _get_db_path().exists():
        return
    sync_index(workspace_dirs)


def search_skills(query: str, workspace_dirs: list[str] | None = None, top_k: int = 5) -> list[dict]:
    """使用 BM25 (FTS5) 根據 query 搜尋最相關的 Top-K 技能。"""
    import re
    import sqlite3

    sync_index_if_needed(workspace_dirs)

    if not query.strip():
        return list_skills(workspace_dirs)[:top_k]

    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        words = re.findall(r"\w+", query)
        if not words:
            return list_skills(workspace_dirs)[:top_k]

        fts_query = " OR ".join(words)
        try:
            cursor = conn.execute(
                "SELECT name, description FROM skills_fts WHERE skills_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, top_k),
            )
            results = [{"name": row[0], "description": row[1]} for row in cursor.fetchall()]
            if not results:
                return list_skills(workspace_dirs)[:top_k]
            return results
        except sqlite3.OperationalError as e:
            log.warning("FTS5 查詢失敗: %s", e)
            return list_skills(workspace_dirs)[:top_k]


# ── 系統提示注入 ──────────────────────────────────


def skills_index_prompt(workspace_dirs: list[str] | None = None, query: str = "") -> str:
    """產生附加到 system prompt 的 skill 目錄；有 query 時執行 BM25 檢索。"""
    if query:
        skills = search_skills(query, workspace_dirs)
    else:
        # 向下相容，如果沒有 query 就給出全部（或依舊限制數量避免溢位）
        skills = list_skills(workspace_dirs)

    if not skills:
        return ""

    lines = [
        "\n\n## 可用的 Skills",
        "以下 skill 是特定任務的操作手冊。當使用者的請求與某個",
        "skill 的描述相關時，先呼叫 use_skill 工具載入完整指示，",
        "再依照指示執行：",
    ]
    for s in skills:
        lines.append(f"- {s['name']}：{s['description']}")

    return "\n".join(lines)


# ── use_skill 工具 ────────────────────────────────

SKILL_TOOL = {
    "type": "function",
    "name": "use_skill",
    "description": (
        "載入指定 skill 的完整指示（操作手冊）。當使用者的請求符合某個 skill 的描述時呼叫，然後依照回傳的指示完成任務。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "skill 名稱（見系統提示中的清單）",
            },
        },
        "required": ["name"],
    },
}


def skill_tools(workspace_dirs: list[str] | None = None) -> list[dict]:
    """有 skill 才註冊工具，避免無謂佔用 context。"""
    return [SKILL_TOOL] if list_skills(workspace_dirs) else []


def is_skill_tool(name: str) -> bool:
    return name == "use_skill"


def call_skill_tool(args: dict, workspace_dirs: list[str] | None = None) -> str:
    name = str(args.get("name", "")).strip()
    body = load_skill(name, workspace_dirs)

    if body is None:
        available = ", ".join(s["name"] for s in list_skills(workspace_dirs)) or "（無）"
        return f"[ERROR] 找不到 skill「{name}」。可用：{available}"

    return f"# Skill: {name}\n\n{body}"
