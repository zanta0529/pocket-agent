import argparse
import sys
from pathlib import Path

# 將專案的 src 目錄動態加入 sys.path
repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root / "src"))

from pocket_agent.tools.document import call_document_tool  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description="將二進位文件 (PDF, DOCX, PPTX) 轉換為 Markdown 格式。",
        usage="python scripts/convert_to_md.py <input_file> [-o <output_file>]",
    )
    parser.add_argument("input", type=str, help="欲轉換的來源檔案路徑 (支援 .pdf, .docx, .pptx)")
    parser.add_argument("-o", "--output", type=str, help="輸出檔案路徑 (預設為與來源檔同目錄下的同名 .md 檔)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists() or not input_path.is_file():
        print(f"[Error] 找不到來源檔案: {input_path}", file=sys.stderr)
        sys.exit(1)

    # 如果未指定輸出路徑，則替換副檔名為 .md
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_suffix(".md")

    print(f"📄 正在解析: {input_path} ...")
    content = call_document_tool("read_document", {"path": str(input_path)})

    if content.startswith("[ERROR]"):
        print(content, file=sys.stderr)
        sys.exit(1)

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"✅ 檔案已成功轉換並儲存至: {output_path}")
    except Exception as e:
        print(f"[Error] 寫入檔案失敗: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
