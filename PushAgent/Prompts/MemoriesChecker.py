import json
from typing import Any

from PushAgent.OpenAiServices import ChatCompletionRequest

SystemPrompt: str = (
    "你是一个严格的一致性判断器。你会收到一份 '上次经验记忆'、一份 '当前 Notion 页内容' 和一份 '用户本次要写入的 commitPreview'。"
    "你的任务是结合用户本次要写入的 commitPreview，判断当前 Notion 页是否仍然符合上次经验记忆，以及给出原因。"
    "'当前 Notion 页' 是一个按某种结构记录工作任务的页，你负责校验的记忆会提供给另一个 Agent"
    "那个 Agent 的任务目标是将用户提供的任务条目信息写入到当前Notion页内"
    "然而，由于Notion页可能有多样的结构，所以后续Agent会整理好这个结构，方面更后续的Agent理解和寻址"
    "经验记忆包含三部分：页总体结构概括，用户可能希望我如何寻址并找到真正记录任务的地方，页内有的子页类型"
    "子页类型是一个列表，但是每个项的数据是自然语言的总结，包含：a. 该类型是否有效 b. 该类型的名字 c. 该类型的描述"
    "你只需要判断结构、寻址方式猜测、子页类型是否一致，并考虑这些经验是否足以服务本次 commitPreview 的写入；"
    "不要因为普通内容增删、任务条目变化、日期变化而判定不一致。"
    "必须只返回 JSON。")

JsonSchema = {
        "type": "json_schema",
        "json_schema": {
            "name": "notion_memory_consistency_check",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "consistent": {"type": "boolean"},
                    "reason": {"type": "string"}
                },
                "required": ["consistent", "reason"],
                "additionalProperties": False
            }
        }
    }

def GetPrompt(memory: str, notionMarkdown: str, commitPreview: str) -> str:
    return (f"""
【上次经验记忆】
{memory}

【用户本次要写入的 commitPreview】
{commitPreview}

【当前 Notion 页内容】
{notionMarkdown}

请判断当前 Notion 页是否与上次经验记忆一致，并判断该经验记忆是否仍然适合指导本次 commitPreview 的写入。
返回字段：
- consistent: boolean
- reason: string
""")

def Run(memory: str, notionMarkdown: str, commitPreview: str, openAiApiKey: str) -> bool:
    prompt = GetPrompt(memory, notionMarkdown, commitPreview)
    cgs = ChatCompletionRequest(apiKey=openAiApiKey, model="gpt-5.5")
    cgs.SetTimeout(1000)
    cgs.SetSystem(SystemPrompt)
    cgs.SetPrompt(prompt)
    cgs.SetTemperature(1)
    cgs.SetResponseFormat(JsonSchema)

    result = cgs.SendAndWait()
    data: dict[str, Any] = json.loads(result.text)
    return bool(data["consistent"])