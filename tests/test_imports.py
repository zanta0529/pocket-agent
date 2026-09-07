"""
基礎冒煙測試 (Smoke Test)
確保專案核心模組與依賴能被正確載入，確保沒有語法錯誤或遺漏依賴。
"""

import sys
from pathlib import Path

# 確保可以 import 根目錄的套件
sys.path.append(str(Path(__file__).parent.parent))


def test_core_imports():
    """測試 Agent 核心後端模組是否可匯入"""
    import agent.auth
    import agent.core
    import agent.hooks
    import agent.skills

    assert agent.core is not None
    assert agent.auth is not None


def test_gui_imports():
    """測試 GUI 核心模組是否可匯入"""
    import gui.app
    import gui.chat

    assert gui.app is not None
