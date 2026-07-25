from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
import asyncio
import json
import os
import threading
import urllib.error
import urllib.request


@dataclass
class ChatCompletionResult:
    raw: dict[str, Any]
    text: str
    toolCalls: list[dict[str, Any]]


class ChatCompletionRequest:
    """Convenient OpenAI Chat Completions request builder.

    Method names intentionally use C#-style PascalCase, while variables and fields use Java-style camelCase.
    """

    def __init__(self, apiKey: str | None = None, model: str | None = None, baseUrl: str | None = None) -> None:
        self.apiKey = apiKey or os.getenv("OPENAI_API_KEY") or ""
        self.model = model or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
        self.baseUrl = (baseUrl or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.timeoutSeconds = 60
        self.messages: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.toolChoice: str | dict[str, Any] | None = None
        self.temperature: float | None = None
        self.maxTokens: int | None = None
        self.responseFormat: dict[str, Any] | None = None
        self.extraPayload: dict[str, Any] = {}
        self.finish = False
        self.running = False
        self.result: ChatCompletionResult | None = None
        self.error: BaseException | None = None
        self.thread: threading.Thread | None = None
        self.completeHandlers: list[Callable[[ChatCompletionResult], None]] = []
        self.errorHandlers: list[Callable[[BaseException], None]] = []

    @property
    def Finish(self) -> bool:
        return self.finish

    @property
    def Running(self) -> bool:
        return self.running

    @property
    def Result(self) -> ChatCompletionResult | None:
        return self.result

    @property
    def Error(self) -> BaseException | None:
        return self.error

    def SetApiKey(self, apiKey: str) -> "ChatCompletionRequest":
        self.apiKey = apiKey
        return self

    def SetBaseUrl(self, baseUrl: str) -> "ChatCompletionRequest":
        self.baseUrl = baseUrl.rstrip("/")
        return self

    def SetModel(self, model: str) -> "ChatCompletionRequest":
        self.model = model
        return self

    def SetTimeout(self, timeoutSeconds: int) -> "ChatCompletionRequest":
        self.timeoutSeconds = timeoutSeconds
        return self

    def SetSystem(self, prompt: str) -> "ChatCompletionRequest":
        return self.AddMessage("system", prompt)

    def SetPrompt(self, prompt: str) -> "ChatCompletionRequest":
        return self.AddMessage("user", prompt)

    def AddUser(self, content: str) -> "ChatCompletionRequest":
        return self.AddMessage("user", content)

    def AddAssistant(self, content: str) -> "ChatCompletionRequest":
        return self.AddMessage("assistant", content)

    def AddMessage(self, role: str, content: str | list[dict[str, Any]]) -> "ChatCompletionRequest":
        self.messages.append({"role": role, "content": content})
        return self

    def AddToolResult(self, toolCallId: str, content: str | dict[str, Any]) -> "ChatCompletionRequest":
        toolContent = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        self.messages.append({"role": "tool", "tool_call_id": toolCallId, "content": toolContent})
        return self

    def AddTool(self, arguments: dict[str, Any]) -> "ChatCompletionRequest":
        if arguments.get("type") == "function" and isinstance(arguments.get("function"), dict):
            self.tools.append(arguments)
            return self

        name = arguments.get("name")
        description = arguments.get("description") or ""
        parameters = arguments.get("parameters") or {"type": "object", "properties": {}}
        if not name:
            raise ValueError("AddTool 需要 name，或传入完整 {'type': 'function', 'function': ...}。")
        self.tools.append(
            {
                "type": "function",
                "function": {
                    "name": str(name),
                    "description": str(description),
                    "parameters": parameters,
                },
            }
        )
        return self

    def AddTools(self, tools: list[dict[str, Any]]) -> "ChatCompletionRequest":
        for tool in tools:
            self.AddTool(tool)
        return self

    def SetToolChoice(self, toolChoice: str | dict[str, Any]) -> "ChatCompletionRequest":
        self.toolChoice = toolChoice
        return self

    def SetTemperature(self, temperature: float) -> "ChatCompletionRequest":
        self.temperature = temperature
        return self

    def SetMaxTokens(self, maxTokens: int) -> "ChatCompletionRequest":
        self.maxTokens = maxTokens
        return self

    def SetJsonMode(self) -> "ChatCompletionRequest":
        self.responseFormat = {"type": "json_object"}
        return self

    def SetResponseFormat(self, responseFormat: dict[str, Any]) -> "ChatCompletionRequest":
        self.responseFormat = responseFormat
        return self

    def SetExtra(self, key: str, value: Any) -> "ChatCompletionRequest":
        self.extraPayload[key] = value
        return self

    def OnComplete(self, handler: Callable[[ChatCompletionResult], None]) -> "ChatCompletionRequest":
        self.completeHandlers.append(handler)
        if self.finish and self.result:
            handler(self.result)
        return self

    def OnError(self, handler: Callable[[BaseException], None]) -> "ChatCompletionRequest":
        self.errorHandlers.append(handler)
        if self.finish and self.error:
            handler(self.error)
        return self

    def BuildPayload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
        }
        if self.tools:
            payload["tools"] = self.tools
        if self.toolChoice is not None:
            payload["tool_choice"] = self.toolChoice
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.maxTokens is not None:
            payload["max_tokens"] = self.maxTokens
        if self.responseFormat is not None:
            payload["response_format"] = self.responseFormat
        payload.update(self.extraPayload)
        return payload

    def Send(self) -> "ChatCompletionRequest":
        if self.running:
            return self
        self.finish = False
        self.running = True
        self.error = None
        self.result = None
        self.thread = threading.Thread(target=self._SendInternal, daemon=True)
        self.thread.start()
        return self

    async def WaitableSend(self) -> ChatCompletionResult:
        return await asyncio.to_thread(self.SendAndWait)

    def SendAndWait(self) -> ChatCompletionResult:
        self._SendInternal()
        if self.error:
            raise self.error
        if not self.result:
            raise RuntimeError("OpenAI 请求已结束，但没有 result。")
        return self.result

    def Wait(self, timeoutSeconds: float | None = None) -> "ChatCompletionRequest":
        if self.thread:
            self.thread.join(timeout=timeoutSeconds)
        return self

    def GetText(self) -> str:
        return self.result.text if self.result else ""

    def GetJson(self) -> dict[str, Any]:
        text = self.GetText().strip()
        if not text:
            return {}
        return json.loads(text)

    def GetToolCalls(self) -> list[dict[str, Any]]:
        return self.result.toolCalls if self.result else []

    def _SendInternal(self) -> None:
        try:
            if not self.apiKey:
                raise RuntimeError("缺少 OPENAI_API_KEY。")
            payload = self.BuildPayload()
            request = urllib.request.Request(
                f"{self.baseUrl}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.apiKey}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeoutSeconds) as response:
                rawText = response.read().decode("utf-8")
            rawData = json.loads(rawText)
            message = rawData.get("choices", [{}])[0].get("message", {})
            text = str(message.get("content") or "")
            toolCalls = message.get("tool_calls") or []
            self.result = ChatCompletionResult(raw=rawData, text=text, toolCalls=toolCalls)
            for handler in list(self.completeHandlers):
                handler(self.result)
        except urllib.error.HTTPError as exc:
            errorBody = exc.read().decode("utf-8", errors="ignore")
            self.error = RuntimeError(f"OpenAI HTTP {exc.code}: {errorBody}")
            for handler in list(self.errorHandlers):
                handler(self.error)
        except BaseException as exc:
            self.error = exc
            for handler in list(self.errorHandlers):
                handler(exc)
        finally:
            self.running = False
            self.finish = True
