import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.auth import fetch_available_models


def test_fetch_available_models_ollama():
    """測試抓取 Ollama 模型時是否能正確處理 OLLAMA_BASE_URL 與 API 回應"""
    # 模擬 httpx.Client 與其 get 方法
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"models": [{"name": "llama3:8b"}, {"name": "qwen2.5:7b"}]}

    mock_client_instance = MagicMock()
    mock_client_instance.get.return_value = mock_response

    # httpx.Client 當作 context manager 使用 (with httpx.Client() as client:)
    mock_client_class = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    with patch("agent.auth.httpx.Client", mock_client_class):
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://192.168.1.100:11434"}):
            models = fetch_available_models(provider="ollama")

            assert len(models) == 2
            assert "llama3:8b" in models
            assert "qwen2.5:7b" in models

            # 驗證 Client 初始化參數
            mock_client_class.assert_called_with(trust_env=False)

            # 驗證打向的 URL 是否正確組合
            mock_client_instance.get.assert_called_once()
            args, kwargs = mock_client_instance.get.call_args
            assert args[0] == "http://192.168.1.100:11434/api/tags"


def test_fetch_available_models_ollama_v1_suffix():
    """測試若 OLLAMA_BASE_URL 以 /v1 結尾，是否能正確處理 (避免打向 /v1/api/tags)"""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"models": [{"name": "gemma:2b"}]}

    mock_client_instance = MagicMock()
    mock_client_instance.get.return_value = mock_response

    mock_client_class = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    with patch("agent.auth.httpx.Client", mock_client_class):
        with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://192.168.1.100:11434/v1"}):
            models = fetch_available_models(provider="ollama")

            assert models == ["gemma:2b"]

            args, kwargs = mock_client_instance.get.call_args
            assert args[0] == "http://192.168.1.100:11434/api/tags"


def test_fetch_available_models_ollama_exception():
    """測試若 Ollama 連線失敗，不應拋出例外，應回傳空陣列"""
    mock_client_instance = MagicMock()
    mock_client_instance.get.side_effect = Exception("Connection Timeout")

    mock_client_class = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client_instance

    with patch("agent.auth.httpx.Client", mock_client_class):
        models = fetch_available_models(provider="ollama")
        assert models == []
