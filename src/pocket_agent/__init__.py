"""Pocket Agent 套件。

.env 必須在其他模組載入前讀進來：本套件多處在模組層級用 os.getenv
取設定（auth 的 ORIGINATOR / USE_SIMPLIFIED_FLOW、core 的 log 等級），
晚一步就全部拿到空值，使用者改了 .env 也毫無反應。

main.py 的 load_dotenv 來不及——它得先 import 本套件才拿得到
env_path()，那時模組層級的 getenv 早就跑完了。paths 沒有相依套件內
其他模組，可以安全地最先載入。
"""

from dotenv import load_dotenv as _load_dotenv

from pocket_agent.system.paths import env_path as _env_path

_load_dotenv(_env_path())

from pocket_agent.core.agent import Agent  # noqa: E402
from pocket_agent.core.files import FileUploader  # noqa: E402
from pocket_agent.extensions.mcp import MCPManager  # noqa: E402
from pocket_agent.system.auth import get_access_token, get_model, get_provider_client, logout  # noqa: E402
from pocket_agent.system.wizard import run_setup  # noqa: E402
from pocket_agent.tools.image import generate_image  # noqa: E402

__all__ = [
    "Agent",
    "get_provider_client",
    "get_model",
    "get_access_token",
    "logout",
    "MCPManager",
    "FileUploader",
    "run_setup",
    "generate_image",
]
