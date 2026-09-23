import argparse
import ast
import sys
from pathlib import Path

# Try to get stdlib modules (Python 3.10+)
STDLIB = sys.stdlib_module_names if hasattr(sys, "stdlib_module_names") else set()

# Mapping from import names to PyPI package names
# (Used when the import name differs from the package name)
PACKAGE_MAPPING = {
    "dotenv": "python-dotenv",
    "PIL": "pillow",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "yaml": "pyyaml",
    "fitz": "pymupdf4llm",
    "bs4": "beautifulsoup4",
    "multipart": "python-multipart",
    "dateutil": "python-dateutil",
    "cv2": "opencv-python",
    "win32clipboard": "pywin32",
}


def get_imports_from_file(file_path: Path) -> set[str]:
    """Extract all top-level module imports from a Python file."""
    imports = set()
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
    except SyntaxError as e:
        print(f"[Warning] Syntax error parsing {file_path}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[Warning] Error reading {file_path}: {e}", file=sys.stderr)
    return imports


def get_requirements(req_file: Path) -> set[str]:
    """Parse requirements.in or requirements.txt and return normalized package names."""
    reqs = set()
    if not req_file.exists():
        return reqs

    with open(req_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # Skip comments and empty lines
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            # Strip environment markers and version specifiers
            line = line.split(";")[0]
            pkg = line.split(">")[0].split("=")[0].split("<")[0].split("~")[0].strip()
            # Normalize package name (pip treats '-' and '_' identically)
            reqs.add(pkg.lower().replace("_", "-"))
    return reqs


def main():
    parser = argparse.ArgumentParser(
        description="Check for missing dependencies in requirements.in.",
        usage="python scripts/check_imports.py [src ...] [options]",
        epilog=(
            "Examples:\n"
            "  python scripts/check_imports.py src scripts\n"
            "  python scripts/check_imports.py src --ignore pytest"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("src", type=str, nargs="*", default=["src"], help="Source directories to scan (default: src)")
    parser.add_argument(
        "--req",
        type=str,
        action="append",
        help="Requirements files to check against (can be used multiple times, default: requirements.in)",
    )
    parser.add_argument("--ignore", type=str, nargs="*", default=[], help="Packages to ignore")
    args = parser.parse_args()

    args.src = args.src or ["src"]
    args.req = args.req or ["requirements.in"]

    repo_root = Path(__file__).parent.parent

    all_imports = set()
    print("🔍 開始依賴套件掃描...")
    for src_path in args.src:
        src_dir = repo_root / src_path
        if not src_dir.exists() or not src_dir.is_dir():
            print(f"  [Warning] Source directory {src_dir} does not exist or is not a directory.", file=sys.stderr)
            continue

        print(f"  📂 掃描目錄: {src_dir}")
        for py_file in src_dir.rglob("*.py"):
            all_imports.update(get_imports_from_file(py_file))

    print("...")

    reqs = set()
    for req_path in args.req:
        req_file = repo_root / req_path
        reqs.update(get_requirements(req_file))

    missing = []
    # Project's own modules (e.g. pocket_agent) should be ignored
    project_module = "pocket_agent"
    ignored_pkgs = set(args.ignore)

    for imp in all_imports:
        # Ignore standard library and private/relative imports
        if imp in STDLIB or imp.startswith("_"):
            continue

        # Map import name to PyPI package name, normalized to use hyphens
        pkg_name = PACKAGE_MAPPING.get(imp, imp).lower().replace("_", "-")

        if pkg_name == project_module.replace("_", "-"):
            continue
        if pkg_name in ignored_pkgs:
            continue

        if pkg_name not in reqs:
            missing.append(imp)

    if missing:
        print(f"❌ 發現可能遺漏的套件 (Imports not found in {args.req}):")
        for m in sorted(missing):
            mapped_name = PACKAGE_MAPPING.get(m, m).lower().replace("_", "-")
            if m.lower().replace("_", "-") != mapped_name:
                print(f" - {m} (PyPI 套件名稱通常為: {mapped_name})")
            else:
                print(f" - {m}")
        sys.exit(1)
    else:
        print(f"✅ 所有使用的第三方套件皆已包含在 {args.req} 中！")
        sys.exit(0)


if __name__ == "__main__":
    main()
