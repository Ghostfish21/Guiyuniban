from PushAgent import Agent
from OpenAiServices import ChatCompletionRequest


agent = PushAgent(configFile="config.txt")

# 访问特定 Notion 页
pageData = agent.AccessNotionPage(title="任务分类")
print(pageData["text"])

# 写入特定内容到 Notion 页
# agent.WriteContentToNotionPage("notion-page-id", "# 今日总结\n- 完成 PushAgent 拆分", heading="日志")

# 写入 Notion database
# agent.WriteRowsToNotionDatabase(
#     "database-id",
#     [
#         {"Name": "写代码", "Hours": 1.5, "Category": "开发"},
#         {"Name": "复盘", "Hours": 0.5, "Category": "整理"},
#     ],
# )

# 好用的 ChatGPT 请求工具
cgs = ChatCompletionRequest()
cgs.SetSystem("你是一个严格输出 JSON 的助手。")
cgs.AddTool(
    {
        "name": "save_task",
        "description": "保存一个任务",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "hours": {"type": "number"},
            },
            "required": ["name"],
        },
    }
)
cgs.SetPrompt("把‘写 PushAgent 1.5 小时’整理为任务。")
cgs.OnComplete(lambda result: print(result.text, result.toolCalls))
cgs.Send()
while not cgs.Finish:
    pass

# async 用法：
# result = await cgs.WaitableSend()

# 同步等待：
# result = cgs.SendAndWait()
