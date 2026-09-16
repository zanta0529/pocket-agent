"""Offline Markdown-to-PDF conversion; never overwrite an existing PDF."""

import re
from html import escape
from pathlib import Path

from markdown_it import MarkdownIt
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    source = root / "dist" / "PocketAgent_Manual.md"
    output = Path(__file__).with_name("PocketAgent_Manual.pdf")
    if output.exists():
        raise FileExistsError(output)
    text = source.read_text(encoding="utf-8")
    pdfmetrics.registerFont(TTFont("CJK", "C:/Windows/Fonts/msjh.ttc", subfontIndex=0))
    pdfmetrics.registerFontFamily("CJK", normal="CJK", bold="CJK", italic="CJK", boldItalic="CJK")
    base = ParagraphStyle("body", fontName="CJK", fontSize=10, leading=16, spaceAfter=6, wordWrap="CJK")
    code = ParagraphStyle(
        "code", parent=base, fontSize=8, leading=12, backColor=colors.HexColor("#f1f5f9"), borderPadding=5
    )
    cell = ParagraphStyle("cell", parent=base, fontSize=8, leading=12, spaceAfter=0)
    headings = {
        i: ParagraphStyle(
            f"h{i}",
            parent=base,
            fontSize=max(11, 22 - i * 2),
            leading=max(17, 28 - i * 2),
            spaceBefore=12,
            spaceAfter=8,
            keepWithNext=True,
            textColor=colors.HexColor("#163454"),
        )
        for i in range(1, 7)
    }
    tokens = MarkdownIt("commonmark").enable("table").parse(text)
    story = []
    counters: list[int | None] = []
    prefix = ""

    def inline(token) -> str:
        result = []
        for child in token.children or []:
            kind = child.type
            if kind in ("text", "code_inline"):
                result.append(escape(child.content))
            elif kind in ("softbreak", "hardbreak"):
                result.append("<br/>")
            elif kind == "html_inline":
                result.append("<br/>" if re.fullmatch(r"<br\s*/?>", child.content) else escape(child.content))
            elif kind == "image":
                result.append(escape(child.content))
            elif kind == "strong_open":
                result.append("<b>")
            elif kind == "strong_close":
                result.append("</b>")
            elif kind == "em_open":
                result.append("<i>")
            elif kind == "em_close":
                result.append("</i>")
            elif kind == "link_open":
                href = child.attrGet("href") or ""
                result.append(
                    f'<link href="{escape(href, quote=True)}" color="#235b91">'
                    if href.startswith(("https://", "http://"))
                    else ""
                )
            elif kind == "link_close":
                # Only external links are clickable; local Markdown links remain labels.
                opened = "".join(result)
                if opened.count("<link ") > opened.count("</link>"):
                    result.append("</link>")
        return "".join(result)

    i = 0
    while i < len(tokens):
        token = tokens[i]
        kind = token.type
        if kind == "heading_open":
            story.append(Paragraph(inline(tokens[i + 1]), headings[int(token.tag[1:])]))
            i += 3
            continue
        if kind == "table_open":
            rows = []
            row = []
            i += 1
            while tokens[i].type != "table_close":
                part = tokens[i]
                if part.type == "tr_open":
                    row = []
                elif part.type == "inline":
                    row.append(Paragraph(inline(part), cell))
                elif part.type == "tr_close":
                    rows.append(row)
                i += 1
            table = Table(rows, colWidths=[(A4[0] - 88) / len(rows[0])] * len(rows[0]), repeatRows=1, hAlign="LEFT")
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2eaf3")),
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bdcbd9")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            story.extend([table, Spacer(1, 10)])
        elif kind in ("fence", "code_block"):
            if token.info.strip() == "mermaid":
                story.append(Paragraph("Mermaid 圖表原始碼（本版未轉繪）", cell))
            for line in token.content.splitlines():
                indentation = len(line) - len(line.lstrip(" "))
                content = "&#160;" * indentation + escape(line.lstrip(" "))
                story.append(Paragraph(content or "&#160;", code))
            story.append(Spacer(1, 8))
        elif kind in ("bullet_list_open", "ordered_list_open"):
            counters.append(int(token.attrGet("start") or 1) if kind == "ordered_list_open" else None)
        elif kind in ("bullet_list_close", "ordered_list_close"):
            counters.pop()
        elif kind == "list_item_open":
            number = counters[-1]
            prefix = "• " if number is None else f"{number}. "
            if number is not None:
                counters[-1] = number + 1
        elif kind == "list_item_close":
            prefix = ""
        elif kind == "inline":
            style = ParagraphStyle("list", parent=base, leftIndent=12 * len(counters)) if counters else base
            story.append(Paragraph(escape(prefix) + inline(token), style))
            prefix = ""
        elif kind == "hr":
            story.append(Spacer(1, 10))
        i += 1

    def footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("CJK", 8)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(44, 24, "PocketAgent 完整操作說明書")
        canvas.drawRightString(A4[0] - 44, 24, str(doc.page))
        canvas.restoreState()

    SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=44,
        leftMargin=44,
        topMargin=40,
        bottomMargin=44,
        title="PocketAgent 完整操作說明書",
        author="PocketAgent",
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    reader = PdfReader(output)
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    expected = ["一、安裝", "二、日常操作", "三、設定與擴充", "四、架構與安全", "五、疑難排解", "終端機渲染"]
    missing = [name for name in expected if name not in extracted]
    print(
        f"PDF: {output}\n"
        f"Pages: {len(reader.pages)}\n"
        f"Bytes: {output.stat().st_size}\n"
        f"Missing section labels: {missing}\n"
        f"Mermaid source blocks preserved: {text.count('```mermaid')}"
    )
    if missing:
        raise RuntimeError("PDF section verification failed")


if __name__ == "__main__":
    main()
