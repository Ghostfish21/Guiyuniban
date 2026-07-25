# PushAgentProject

这个项目按你的规则拆分：

- `PushAgent.py`：总入口类 `PushAgent`。
- `NotionServices/`：每个主功能一个文件。共享 Notion 请求、文本转换、配置读取被抽到公共文件。
- `OpenAiServices/`：ChatGPT 请求工具，支持同步、异步等待、回调、后台发送、轮询完成状态。

## 命名规则

- 方法名：C# 风格 PascalCase，例如 `SetSystem`、`WriteContentToNotionPage`。
- 其他名称：Java 风格；类名 PascalCase，变量/字段 camelCase。

## NotionServices 拆分

- `AccessPageService.py`：访问特定 Notion 页，读取正文。
- `ReadTaskCategoriesService.py`：读取“任务分类”页面。
- `WritePageService.py`：写入特定内容到 Notion 页面。
- `WriteDatabaseService.py`：写入 rows 到 Notion database。
- `PageStructureService.py`：创建子页、复制页面、复制块、复制 database schema。
- `PushTasksService.py`：读取 commit preview，把任务 push 到 Notion 页面或 database。
- `NotionClient.py`：共享 Notion HTTP/API 逻辑。
- `NotionText.py`：共享富文本、Markdown block 转换逻辑。
- `NotionContext.py`：共享配置读取逻辑。

## OpenAiServices 用法

```python
from OpenAiServices import ChatCompletionRequest

cgs = ChatCompletionRequest()
cgs.SetSystem("prompt")
cgs.AddTool({
    "name": "save_task",
    "description": "保存任务",
    "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
})
cgs.SetPrompt("设置问题")

# 方式 1：异步等待
# result = await cgs.WaitableSend()

# 方式 2：回调 + 后台发送
cgs.OnComplete(lambda result: print(result.text))
cgs.Send()

# 方式 3：轮询
while not cgs.Finish:
    pass

# 方式 4：同步等待
# result = cgs.SendAndWait()
```

## PushAgent 用法

```python
from PushAgent import Agent

agent = PushAgent(configFile="config.txt")
categoryText = agent.ReadTaskCategoriesFromNotion()
agent.WriteContentToNotionPage("page-id", "# 标题\n- 内容")
```
