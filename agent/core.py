"""Agent 主迴圈：Responses API + 反思推理能力。"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from openai import NOT_GIVEN, AsyncOpenAI

from .paths import is_packaged, user_dir

# ── Log 檔設定 ────────────────────────────────────
# 開發時寫在執行目錄；發佈版寫 ~/.pocket-agent/，因為程式所在目錄
# 未必可寫（共用磁碟、Program Files），而且 cwd 會隨捷徑而變。
_log_path = user_dir() / "agent.log" if is_packaged() else Path("agent.log")

_handlers: list[logging.Handler] = [
    logging.FileHandler(_log_path, encoding="utf-8"),
]

# 可攜版用 pythonw.exe 啟動且無 console，此時 sys.stderr 是 None。
# 無條件掛 StreamHandler 會讓第一筆 log 就 AttributeError 而整個程式
# 靜默死掉（沒有 console 可以顯示錯誤，症狀是雙擊後完全沒反應）。
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler())

# 預設 INFO。要深入追查時設環境變數 MY_AGENT_LOG_LEVEL=DEBUG，
# 不要把 DEBUG 當成預設發佈出去（理由見下方第三方 logger 的說明）。
_LOG_LEVEL = os.getenv("MY_AGENT_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# 這些套件在 DEBUG 會把完整的 HTTP 標頭與回應主體寫進 log，
# 其中包含 OAuth token 與 session cookie——等於在每台機器上留下
# 一份使用者憑證的明文副本。無論主層級設多低都壓到 WARNING。
# PIL 則是每讀一張圖就吐十幾行 PNG chunk，純粹是雜訊。
for _noisy in (
    "httpx",
    "httpcore",
    "openai",
    "PIL",
    "urllib3",
    "asyncio",
    "keyring",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

log = logging.getLogger("pocket-agent")

from .doctools import DOC_TOOLS, call_doc_tool, is_doc_tool  # noqa: E402
from .files import FileUploader  # noqa: E402
from .hooks import run_hooks  # noqa: E402
from .imagegen import disable_model_param, image_tool, save_image_b64  # noqa: E402
from .mcp_manager import MCPManager  # noqa: E402
from .shell import SHELL_TOOLS, call_shell_tool, is_shell_tool  # noqa: E402
from .skills import call_skill_tool, is_skill_tool, skill_tools, skills_index_prompt  # noqa: E402
from .subagents import get_subagent, is_subagent_tool, subagent_tools  # noqa: E402

DEFAULT_MAX_TOOL_ROUNDS = 10


class Agent:
    def __init__(
        self,
        client: AsyncOpenAI,
        mcp: MCPManager,
        model: str | None = None,
        system_prompt: str = "You are a helpful assistant.",
        reasoning_effort: str = "medium",  # none/low/medium/high/xhigh
        max_tool_rounds: int | None = None,  # 未指定時讀 MAX_TOOL_ROUNDS 環境變數
        is_subagent: bool = False,  # 子代理不觸發 hooks、不能再開子代理
    ):
        self.client = client
        self.mcp = mcp
        self._is_subagent = is_subagent
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-6-astra")
        self.system_prompt = system_prompt
        self.reasoning_effort = reasoning_effort
        self.max_tool_rounds = max_tool_rounds or int(os.getenv("MAX_TOOL_ROUNDS", DEFAULT_MAX_TOOL_ROUNDS))
        self.files = FileUploader(client)
        self.history: list[dict] = []
        self.cancel_requested = False  # 使用者按「停止」時設為 True（跨執行緒安全）
        self.last_images: list[Path] = []  # 本輪 chat 產生的圖片路徑（GUI 顯示用）
        self.last_files: list[Path] = []  # 本輪 chat 產生的文件路徑（GUI 顯示用）
        self.workspace_dirs: list[str] = []  # CLI 與 GUI 共用的工作區路徑清單

        # 串流回呼：callable(kind, data)，從背景 asyncio thread 呼叫，
        # GUI 端需自行轉回主執行緒。kind 與 data 的對應：
        #   round_start  data=""                    工具呼叫後重跑，清掉上一輪暫定文字
        #   thinking     data=推理片段（str）
        #   text         data=回覆片段（str）
        #   tool_start   data={call_id, name, arguments}
        #   tool_done    data={call_id, name, result, ok}
        self.on_stream = None

        # CLI 指令確認回呼：callable(command) -> bool。
        # None 時不確認直接執行；GUI 會掛上跳確認視窗的實作。
        self.on_confirm = None

    def _emit(self, kind: str, data="") -> None:
        callback = self.on_stream
        if callback is None:
            return
        try:
            callback(kind, data)
        except Exception:
            log.exception("on_stream callback 失敗")

    # ── Public API ────────────────────────────────

    async def chat(
        self,
        user_input: str,
        attachments: list[str] | None = None,
    ) -> tuple[str, str, str]:
        """
        回傳 (thinking, answer, usage_str)。
        thinking：模型的推理過程（可能為空字串）
        answer：最終回覆
        usage_str：耗時與 token 數的格式化字串
        """
        import time

        from openai import BadRequestError

        self.cancel_requested = False
        self.total_tokens = 0
        start_time = time.time()

        if not self._is_subagent:
            await asyncio.to_thread(run_hooks, "before_send", {"user_input": user_input})

        content = await self._build_content(user_input, attachments or [])
        self.history.append({"role": "user", "content": content})

        self.last_images = []
        self.last_files = []

        try:
            thinking, reply = await self._run_loop()
        except BadRequestError as e:
            err_msg = str(e).lower()
            if (
                "context_length_exceeded" in err_msg
                or "context length" in err_msg
                or "too large" in err_msg
                or "maximum context" in err_msg
            ):
                log.warning("Context exceeded intercepted.")
                thinking = ""
                reply = (
                    "⚠️ **Context Exceeded**\n\n"
                    "輸入的內容或附加檔案過長，已超出當前模型的上下文處理極限。\n"
                    "建議做法：請縮小附加的檔案數量或拆分內容後再試一次。"
                )
                self.history.pop()  # Remove the failed user input from history
            else:
                raise

        if self.last_images:
            paths = "\n".join(f"🖼️ {p}" for p in self.last_images)
            reply = (f"{reply}\n\n" if reply.strip() else "") + f"圖片已存檔：\n{paths}"
        if self.last_files:
            paths = "\n".join(f"📄 {p}" for p in self.last_files)
            reply = (f"{reply}\n\n" if reply.strip() else "") + f"檔案已產生：\n{paths}"

        elapsed = time.time() - start_time
        if self.total_tokens:
            usage_str = f"Thought for {elapsed:.1f}s, {self.total_tokens} tokens"
        else:
            usage_str = f"Thought for {elapsed:.1f}s"

        self.history.append({"role": "assistant", "content": reply})

        if not self._is_subagent:
            await asyncio.to_thread(
                run_hooks,
                "after_response",
                {"user_input": user_input, "reply": reply},
            )

        return thinking, reply, usage_str

    def request_stop(self) -> None:
        """要求停止本輪工作（從 GUI 執行緒呼叫）。迴圈會在安全點檢查並收尾。"""
        self.cancel_requested = True

    @staticmethod
    def _stopped_reply(text: str) -> str:
        return (f"{text}\n\n" if text.strip() else "") + "⏹ 已由使用者停止"

    def clear_history(self) -> None:
        self.history.clear()

    # ── Core loop ─────────────────────────────────

    def _build_tools(self) -> list:
        """MCP 工具 + hosted 生圖工具 + 本地文件工具 + skill / subagent 工具。"""
        tools = list(self.mcp.openai_tools() or []) + DOC_TOOLS + SHELL_TOOLS + skill_tools()

        url_str = str(self.client.base_url)
        is_openai = "openai.com" in url_str or "chatgpt.com" in url_str
        if is_openai:
            tools.insert(0, image_tool())

        ask_question = {
            "type": "function",
            "name": "ask_question",
            "description": (
                "Use this tool to ask the user one or more multiple-choice questions to clarify "
                "requirements or solicit design feedback. This will block execution and show a "
                "modal dialog to the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
                                "options": {"type": "array", "items": {"type": "string"}},
                                "is_multi_select": {"type": "boolean"},
                            },
                            "required": ["question", "options"],
                        },
                    }
                },
                "required": ["questions"],
            },
        }
        tools.append(ask_question)

        # 子代理不能再開子代理，避免無限遞迴
        if not self._is_subagent:
            tools += subagent_tools()
        return tools

    def _instructions(self) -> str:
        """system prompt + skill 目錄（每輪重算，skill 可熱加）。"""
        return self.system_prompt + skills_index_prompt()

    async def _run_subagent(self, args: dict) -> str:
        """委派任務給子代理，回傳其最終回覆。"""
        name = str(args.get("name", "")).strip()
        task = str(args.get("task", "")).strip()

        sa = get_subagent(name)
        if sa is None:
            from .subagents import list_subagents

            available = "、".join(a["name"] for a in list_subagents()) or "（無）"
            return f"[ERROR] 找不到子代理「{name}」。可用：{available}"
        if not task:
            return "[ERROR] 沒有提供交辦任務。"

        sub = Agent(
            client=self.client,
            mcp=self.mcp,
            model=sa["model"] or self.model,
            system_prompt=sa["prompt"] or "You are a helpful assistant.",
            reasoning_effort=sa["reasoning_effort"] or self.reasoning_effort,
            max_tool_rounds=self.max_tool_rounds,
            is_subagent=True,
        )
        log.info("SUBAGENT run %s task=%r", name, task[:80])

        try:
            _thinking, reply, _usage_str = await sub.chat(task)
        except Exception as e:
            log.exception("subagent 執行失敗")
            return f"[ERROR] 子代理「{name}」執行失敗：{e}"

        return f"【子代理 {name} 的結果】\n{reply}"

    async def _run_loop(self) -> tuple[str, str]:
        tools = self._build_tools()

        # Context Overflow 保護機制 (Sliding Window)
        history_str_len = sum(len(str(msg)) for msg in self.history)
        if history_str_len > 120000 and len(self.history) > 14:
            notice = {"role": "system", "content": "[System] 中間的對話歷史已被系統自動截斷以避免 Context Overflow。"}
            self.history = self.history[:4] + [notice] + self.history[-10:]

        input_items: list = list(self.history)

        # reasoning 參數：effort 為 "none" 時不送推理
        reasoning_param = NOT_GIVEN
        if self.reasoning_effort and self.reasoning_effort != "none":
            reasoning_param = {
                "effort": self.reasoning_effort,
                "summary": "auto",
            }

        consecutive_errors = 0  # 連續全錯輪次計數

        for _ in range(self.max_tool_rounds):
            if self.cancel_requested:
                return "", self._stopped_reply("")

            thinking_chunks: list[str] = []
            text_chunks: list[str] = []
            func_calls: dict[str, dict] = {}

            # 新一輪開始（工具呼叫後重跑時，讓 GUI 清掉上一輪的暫定文字）
            self._emit("round_start")

            try:
                stream = await self.client.responses.create(
                    model=self.model,
                    input=input_items,
                    instructions=self._instructions(),
                    tools=tools,
                    store=False,
                    stream=True,
                    reasoning=reasoning_param,
                )
            except Exception as e:
                # 後端可能不吃 image_generation 工具的 model 參數 → 拿掉重試一次
                if "model" in str(e).lower() or "unknown" in str(e).lower():
                    disable_model_param()
                    tools = self._build_tools()
                    stream = await self.client.responses.create(
                        model=self.model,
                        input=input_items,
                        instructions=self._instructions(),
                        tools=tools,
                        store=False,
                        stream=True,
                        reasoning=reasoning_param,
                    )
                else:
                    raise

            async for event in stream:
                if self.cancel_requested:
                    try:
                        await stream.close()
                    except Exception:
                        pass
                    break

                etype = getattr(event, "type", "")

                # ── 推理摘要（思考過程）────────────────
                if etype == "response.reasoning_summary_text.delta":
                    delta = getattr(event, "delta", "")
                    thinking_chunks.append(delta)
                    self._emit("thinking", delta)

                # ── 最終回覆文字 ──────────────────────
                elif etype == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    text_chunks.append(delta)
                    self._emit("text", delta)

                # ── 工具呼叫：開始 ────────────────────
                elif etype == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", "") == "function_call":
                        cid = item.call_id
                        func_calls[cid] = {
                            "call_id": cid,
                            "name": item.name,
                            "arguments": getattr(item, "arguments", "") or "",
                        }
                        log.debug(
                            "TOOL item.added call_id=%s name=%s args=%r", cid, item.name, func_calls[cid]["arguments"]
                        )

                # ── 工具呼叫：參數串流 ─────────────────
                elif etype == "response.function_call_arguments.delta":
                    cid = getattr(event, "call_id", None)
                    delta = getattr(event, "delta", "")
                    log.debug("TOOL args.delta call_id=%s delta=%r", cid, delta)
                    if cid and cid in func_calls:
                        func_calls[cid]["arguments"] += delta

                # ── 工具呼叫：完成（以完整 arguments 覆蓋 delta 累積值）
                elif etype == "response.output_item.done":
                    item = getattr(event, "item", None)
                    if item and getattr(item, "type", "") == "function_call":
                        cid = item.call_id
                        full_args = getattr(item, "arguments", "") or ""
                        log.debug("TOOL item.done call_id=%s full_args=%r", cid, full_args)
                        if cid in func_calls:
                            func_calls[cid]["arguments"] = full_args
                    # ── 生圖完成：base64 PNG 存檔 ──────────
                    elif item and getattr(item, "type", "") == "image_generation_call":
                        b64 = getattr(item, "result", None)
                        if b64:
                            path = save_image_b64(b64)
                            self.last_images.append(path)
                            log.info("IMAGE saved %s", path)

                # ── 串流結束，提取用量 ────────────────
                elif etype == "response.done":
                    usage = getattr(getattr(event, "response", None), "usage", None)
                    if usage:
                        if isinstance(usage, dict):
                            self.total_tokens += usage.get("total_tokens", 0)
                        else:
                            self.total_tokens += getattr(usage, "total_tokens", 0)

            thinking = "".join(thinking_chunks)
            text = "".join(text_chunks)
            fc_list = list(func_calls.values())

            if self.cancel_requested:
                return thinking, self._stopped_reply(text)

            if not fc_list:
                return thinking, text

            # 執行工具，下一輪繼續
            all_errors = True
            for fc in fc_list:
                if self.cancel_requested:
                    return thinking, self._stopped_reply(text)
                self._emit(
                    "tool_start",
                    {
                        "call_id": fc["call_id"],
                        "name": fc["name"],
                        "arguments": fc["arguments"],
                    },
                )

                try:
                    raw = fc["arguments"].strip()
                    args = json.loads(raw) if raw else {}
                    log.debug("TOOL calling %s args=%r", fc["name"], args)

                    if not self._is_subagent:
                        await asyncio.to_thread(
                            run_hooks,
                            "before_tool",
                            {"tool_name": fc["name"], "tool_args": raw},
                        )

                    if is_skill_tool(fc["name"]):
                        result = call_skill_tool(args)
                    elif fc["name"] == "ask_question":
                        loop = asyncio.get_running_loop()
                        fut = loop.create_future()

                        def set_result_threadsafe(res):
                            loop.call_soon_threadsafe(fut.set_result, res)

                        self._emit(
                            "ask_question",
                            {
                                "call_id": fc["call_id"],
                                "questions": args.get("questions", []),
                                "callback": set_result_threadsafe,
                            },
                        )
                        result_dict = await fut
                        result = json.dumps(result_dict, ensure_ascii=False)
                    elif is_subagent_tool(fc["name"]):
                        result = await self._run_subagent(args)
                    elif is_shell_tool(fc["name"]):
                        # 阻塞的 subprocess + 確認視窗等待 → 丟到 thread 跑
                        result = await asyncio.to_thread(call_shell_tool, fc["name"], args, self.on_confirm)
                    elif is_doc_tool(fc["name"]):
                        result, fpath = call_doc_tool(fc["name"], args)
                        if fpath:
                            self.last_files.append(fpath)
                    else:
                        result = await self.mcp.call(fc["name"], args)
                    log.debug("TOOL result=%r", result)

                    if not self._is_subagent:
                        await asyncio.to_thread(
                            run_hooks,
                            "after_tool",
                            {"tool_name": fc["name"], "tool_result": result},
                        )
                    if not result.startswith("Error:") and not result.startswith("[ERROR]"):
                        all_errors = False
                except Exception as e:
                    log.error("TOOL error %s: %s", type(e).__name__, e)
                    result = f"[ERROR] {e}"

                self._emit(
                    "tool_done",
                    {
                        "call_id": fc["call_id"],
                        "name": fc["name"],
                        "result": result,
                        "ok": not (result.startswith("Error:") or result.startswith("[ERROR]")),
                    },
                )

                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": fc["call_id"],
                        "name": fc["name"],
                        "arguments": fc["arguments"],
                    }
                )
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": fc["call_id"],
                        "output": result,
                    }
                )

            # 連續兩輪全部都是錯誤 → 強制讓模型直接回報給使用者
            if all_errors:
                consecutive_errors += 1
                if consecutive_errors >= 2:
                    log.warning("TOOL 連續 %d 輪全部錯誤，強制跳出", consecutive_errors)
                    input_items.append(
                        {
                            "role": "user",
                            "content": (
                                "工具連續回傳錯誤。請根據錯誤訊息：\n"
                                "1. 告訴使用者問題的原因\n"
                                "2. 提供具體的修復步驟\n"
                                "3. 不要再呼叫工具"
                            ),
                        }
                    )
                    break
            else:
                consecutive_errors = 0

        return "", "（已達工具呼叫上限）"

    # ── Helpers ───────────────────────────────────

    async def _build_content(self, text: str, attachments: list[str]) -> list | str:
        if not attachments:
            return text
        parts: list = [{"type": "input_text", "text": text}]
        for path in attachments:
            self._emit("read", path)
            result = await self.files.upload(path)
            # PDF 內嵌圖片時 upload() 回傳「文字+多張圖片」的 list，其餘情況是單一 dict
            if isinstance(result, list):
                parts.extend(result)
            else:
                parts.append(result)
        return parts
