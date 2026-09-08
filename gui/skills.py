"""Skill 管理頁面：列出、新增、編輯、刪除 skills/<name>/SKILL.md。"""

import shutil
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from agent.skills import REPO_SKILLS_DIR, list_skills


class SkillsView(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app

        # 編輯中的 SKILL.md 路徑；None 代表新增模式
        self._editing_path: Path | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build()

    # ── 介面 ─────────────────────────────────────

    def _build(self):
        # 標題列
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=30, pady=(28, 6))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="Skills",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            header,
            text="匯入 ZIP",
            width=100,
            fg_color=("gray60", "gray35"),
            hover_color=("gray50", "gray25"),
            command=self._import_zip,
        ).grid(row=0, column=1, sticky="e", padx=(0, 8))

        ctk.CTkButton(
            header,
            text="＋ 新增 Skill",
            width=120,
            command=self._new,
        ).grid(row=0, column=2, sticky="e")

        ctk.CTkLabel(
            self,
            text="Skill 是教模型「怎麼做某類任務」的操作手冊，模型會在相關時自動載入。新增後立即生效，不需重啟。",
            text_color="gray",
            font=ctk.CTkFont(size=12),
            anchor="w",
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=30, pady=(0, 8))

        # Skill 清單
        self._list_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._list_frame.grid(row=2, column=0, sticky="nsew", padx=24, pady=(0, 8))
        self._list_frame.grid_columnconfigure(0, weight=1)

        # 編輯器（預設隱藏）
        self._editor = ctk.CTkFrame(self)
        self._editor.grid(row=3, column=0, sticky="ew", padx=30, pady=(0, 16))
        self._editor.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            self._editor,
            text="名稱",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=0, padx=(16, 8), pady=(14, 4), sticky="w")
        self.name_entry = ctk.CTkEntry(
            self._editor,
            placeholder_text="例：weekly-report（英數與連字號）",
        )
        self.name_entry.grid(row=0, column=1, padx=(0, 16), pady=(14, 4), sticky="ew")

        ctk.CTkLabel(
            self._editor,
            text="描述",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=1, column=0, padx=(16, 8), pady=4, sticky="w")
        self.desc_entry = ctk.CTkEntry(
            self._editor,
            placeholder_text="一句話說明「什麼情況該用」，模型靠這句判斷",
        )
        self.desc_entry.grid(row=1, column=1, padx=(0, 16), pady=4, sticky="ew")

        ctk.CTkLabel(
            self._editor,
            text="內容",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=2, column=0, padx=(16, 8), pady=4, sticky="nw")
        self.body_box = ctk.CTkTextbox(self._editor, height=180, wrap="word")
        self.body_box.grid(row=2, column=1, padx=(0, 16), pady=4, sticky="ew")

        btn_row = ctk.CTkFrame(self._editor, fg_color="transparent")
        btn_row.grid(row=3, column=1, padx=(0, 16), pady=(4, 14), sticky="e")

        ctk.CTkButton(
            btn_row,
            text="取消",
            width=80,
            fg_color=("gray60", "gray35"),
            hover_color=("gray50", "gray25"),
            command=self._hide_editor,
        ).grid(row=0, column=0, padx=(0, 8))

        ctk.CTkButton(
            btn_row,
            text="儲存",
            width=100,
            command=self._save,
        ).grid(row=0, column=1)

        self._editor.grid_remove()
        self.refresh()

    # ── 清單 ─────────────────────────────────────

    def refresh(self):
        for widget in self._list_frame.winfo_children():
            widget.destroy()

        skills = list_skills()

        if not skills:
            ctk.CTkLabel(
                self._list_frame,
                text="還沒有任何 skill，按右上角「＋ 新增 Skill」建立第一個。",
                text_color="gray",
            ).grid(row=0, column=0, pady=24)
            return

        for row_index, skill in enumerate(skills):
            card = ctk.CTkFrame(self._list_frame)
            card.grid(row=row_index, column=0, sticky="ew", padx=6, pady=5)
            card.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                card,
                text=skill["name"],
                font=ctk.CTkFont(size=14, weight="bold"),
                anchor="w",
            ).grid(row=0, column=0, padx=16, pady=(10, 0), sticky="w")

            ctk.CTkLabel(
                card,
                text=skill["description"] or "（沒有描述）",
                text_color="gray",
                font=ctk.CTkFont(size=12),
                anchor="w",
            ).grid(row=1, column=0, padx=16, pady=(0, 2), sticky="w")

            source = "內建技能" if REPO_SKILLS_DIR in skill["path"].parents else "個人技能"
            ctk.CTkLabel(
                card,
                text=f"[{source}]",
                text_color=("gray55", "gray45"),
                font=ctk.CTkFont(size=10),
                anchor="w",
            ).grid(row=2, column=0, padx=16, pady=(0, 10), sticky="w")

            ctk.CTkButton(
                card,
                text="編輯",
                width=64,
                command=lambda s=skill: self._edit(s),
            ).grid(row=0, column=1, rowspan=3, padx=(4, 4))

            ctk.CTkButton(
                card,
                text="刪除",
                width=64,
                fg_color=("gray60", "gray35"),
                hover_color=("#b3261e", "#8c1d18"),
                command=lambda s=skill: self._delete(s),
            ).grid(row=0, column=2, rowspan=3, padx=(0, 12))

    # ── 新增 / 編輯 ───────────────────────────────

    def _new(self):
        self._editing_path = None
        self.name_entry.delete(0, "end")
        self.desc_entry.delete(0, "end")
        self.body_box.delete("1.0", "end")
        self.body_box.insert(
            "1.0",
            "# 指南標題\n\n（在這裡寫給模型的完整指示：步驟、格式、範例、注意事項。）\n",
        )
        self._editor.grid()
        self.name_entry.focus_set()

    def _edit(self, skill: dict):
        try:
            from agent.registry import parse_frontmatter

            meta, body = parse_frontmatter(skill["path"].read_text(encoding="utf-8"))
        except Exception as e:
            messagebox.showerror("讀取失敗", str(e))
            return

        self._editing_path = skill["path"]
        self.name_entry.delete(0, "end")
        self.name_entry.insert(0, meta.get("name", skill["name"]))
        self.desc_entry.delete(0, "end")
        self.desc_entry.insert(0, meta.get("description", ""))
        self.body_box.delete("1.0", "end")
        self.body_box.insert("1.0", body.strip() + "\n")
        self._editor.grid()

    def _hide_editor(self):
        self._editor.grid_remove()
        self._editing_path = None

    def _save(self):
        name = self.name_entry.get().strip()
        desc = self.desc_entry.get().strip()
        body = self.body_box.get("1.0", "end-1c").strip()

        if not name or any(c in name for c in r'\/:*?"<>| '):
            messagebox.showwarning(
                "名稱無效",
                '請輸入 skill 名稱（不能含空白或 \\ / : * ? " < > |）。',
            )
            return
        if not desc:
            messagebox.showwarning(
                "缺少描述",
                "描述是模型判斷「何時使用」的依據，請簡短填寫。",
            )
            return

        # 編輯既有 skill 時存回原目錄；新增時存到專案 skills/
        if self._editing_path is not None:
            base_dir = self._editing_path.parent.parent
        else:
            base_dir = REPO_SKILLS_DIR

        target = base_dir / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n",
            encoding="utf-8",
        )

        # 改名時移除舊目錄
        if (
            self._editing_path is not None
            and self._editing_path.parent != target.parent
            and self._editing_path.exists()
        ):
            shutil.rmtree(self._editing_path.parent, ignore_errors=True)

        self._hide_editor()
        self.refresh()

    def _import_zip(self):
        """匯入 Agent Skills 格式 zip（相容 ChatGPT / Claude 匯出）。"""
        from tkinter import filedialog

        from agent.skills import import_skill_zip

        zip_path = filedialog.askopenfilename(
            title="選擇 skill zip 檔",
            filetypes=[("Zip 檔", "*.zip"), ("所有檔案", "*.*")],
        )
        if not zip_path:
            return

        try:
            names = import_skill_zip(zip_path)
        except FileExistsError as e:
            if not messagebox.askyesno(
                "Skill 已存在",
                f"以下 skill 已存在：{e}\n要覆蓋嗎？",
            ):
                return
            try:
                names = import_skill_zip(zip_path, overwrite=True)
            except Exception as e2:
                messagebox.showerror("匯入失敗", str(e2))
                return
        except Exception as e:
            messagebox.showerror("匯入失敗", str(e))
            return

        messagebox.showinfo(
            "匯入完成",
            "已匯入 skill：" + "、".join(names),
        )
        self.refresh()

    def _delete(self, skill: dict):
        if not messagebox.askyesno(
            "刪除 Skill",
            f"確定要刪除「{skill['name']}」嗎？\n{skill['path'].parent}",
        ):
            return

        shutil.rmtree(skill["path"].parent, ignore_errors=True)
        self.refresh()

    # ── App callback ─────────────────────────────

    def on_show(self):
        self.refresh()
