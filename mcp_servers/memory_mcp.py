import json
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("memory")


def get_memory_file() -> Path:
    # 預設放置於 ~/.pocket-agent/memory.json，或是透過環境變數指定
    env_path = os.getenv("MEMORY_PATH")
    if env_path:
        return Path(env_path)

    d = Path.home() / ".pocket-agent"
    d.mkdir(parents=True, exist_ok=True)
    return d / "memory.json"


def load_memory() -> dict:
    fpath = get_memory_file()
    if not fpath.exists():
        return {}
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_memory(data: dict):
    fpath = get_memory_file()
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@mcp.tool()
def memory_store_fact(key: str, value: str) -> str:
    """儲存一項跨對話的長期記憶或偏好設定"""
    data = load_memory()
    data[key] = value
    save_memory(data)
    return f"已成功儲存記憶：{key} = {value}"


@mcp.tool()
def memory_retrieve_context() -> str:
    """檢索目前儲存的所有長期記憶與上下文"""
    data = load_memory()
    if not data:
        return "目前沒有任何長期記憶。"
    return "長期記憶清單：\n" + "\n".join(f"- {k}: {v}" for k, v in data.items())


@mcp.tool()
def memory_delete_fact(key: str) -> str:
    """刪除一項特定的記憶"""
    data = load_memory()
    if key in data:
        del data[key]
        save_memory(data)
        return f"已刪除記憶：{key}"
    return f"找不到記憶項目：{key}"


if __name__ == "__main__":
    mcp.run()
