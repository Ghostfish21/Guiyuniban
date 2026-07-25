# `log chat` 需求规格

## 1. 命令

```
log chat <用户的中文指令>
```

- `<用户的中文指令>`：用户想让 LLM 对当前 commit_preview 做的修改说明。例如："把周三的写代码任务的类别改成 Work"。
- 空指令直接报错退出，参考 `log start` 空任务名处理方式。

## 2. 前置校验

按顺序：

1. 指令非空。
2. `commit_preview.txt` 存在且非空。
3. `commit_preview.txt` 能被 `_extract_commit_payload` 提取出机器可读 JSON payload；否则报错要求先重跑 `log commit`。

失败均通过 `_render_error` 输出，与 `log push` 一致。

## 3. 完整流程

```
log chat <用户指令>
│
├─ Step 1  前置校验（第 2 节）
│
├─ Step 2  读 commit_preview 的 JSON payload → items
│         将 items 格式化为自然语言列表（第 4.1 节格式）
│
├─ Step 3  调用一次 LLM（无迭代循环）：
│           - system prompt：角色、可改字段、不可改字段（尤其强调 编号）、
│             一致性约束、"只对需要修改的任务返回，未修改的不要出现"
│           - user prompt：<用户指令> + 自然语言列表
│           - structured output 强约束返回 shape（第 4.2 节）
│
├─ Step 4  本地过滤 LLM 输出：
│           a) 剔除编号不在原 items 内的项 → 打印
│              "⚠ LLM 返回的编号 {X} 不在预览内，已忽略"
│           b) 剔除 LLM 试图变更编号的项（对比自身编号字段与被引用的编号）→ 打印
│              "⚠ LLM 试图修改编号 {旧}→{新}，已忽略该条"
│              （即便 structured output 要求返回编号，也要做保险比对）
│           c) 剔除所有字段都未变的项 → 打印
│              "编号 {X} LLM 未提出实际改动，跳过"
│           如过滤后为空，打印"LLM 未提出任何修改"退出。
│
├─ Step 5  一致性违反检测（第 5 节）：
│           对每条剩余改动，检查 结束时间 - 开始时间 是否等于 持续小时。
│           不一致的条目：在 Step 6 的 diff 面板顶部加一行醒目警告，
│           包含编号 + 任务名 + 三个数值。不阻断、不自动修正。
│
├─ Step 6  逐条 y/n 审核（第 8 节 UI 规范）：
│           每条改动一个 rich Panel：
│             - Panel 标题：编号 + 任务名
│             - （如有）一致性警告
│             - 表格：字段 | 变更前 | 变更后，只列有变化的字段
│             - 内容不截断（overflow="fold"）
│           使用 input() 收 y/n；y=接受，n=拒绝（原条目保留原样）。
│
├─ Step 7  构建最终 items（顺序、编号保持不变）：
│           - 接受的编号 → 用改后 item 替换旧的（编号沿用旧的）
│           - 拒绝的编号 → 原样保留
│           - 未被 LLM 提及的编号 → 原样保留
│         重建 commit_preview.txt：
│           - 用最终 items 调 _build_commit_preview_text
│           - commit_payload["commit_id"] 保持不变
│           - commit_payload["generated_at"] 更新为当前时间
│           - 编号绝不重新分配（不调 _ensure_commit_item_indexes；
│             或传 flag 跳过分配逻辑）
│           - task_index.txt 计数器文件不动
│         整体覆盖写 commit_preview.txt。
│
└─ Step 8  兜底重叠检查（第 6 节）
```

## 4. LLM 数据契约

### 4.1 输入：自然语言列表（不用 JSON）

发送给 LLM 的用户消息内容示例：

```
用户指令：把周三的写代码任务类别改成 Work。

当前 commit preview（共 3 条）：
1. 编号: 10000；任务名: 写代码；周几: 周三；持续小时: 2；开始时间: 2026-07-14T09:00:00-04:00；结束时间: 2026-07-14T11:00:00-04:00；类别: 未分类
2. 编号: 10005；任务名: 开会；周几: 周四；持续小时: 1；开始时间: 2026-07-15T10:00:00-04:00；结束时间: 2026-07-15T11:00:00-04:00；类别: Meeting
3. 编号: 10010；任务名: 复盘；周几: 周五；持续小时: 0.5；开始时间: 2026-07-16T18:00:00-04:00；结束时间: 2026-07-16T18:30:00-04:00；类别: Review
```

用中文分号分隔字段。字段顺序固定：编号 → 任务名 → 周几 → 持续小时 → 开始时间 → 结束时间 → 类别。

### 4.2 输出：structured output schema

**JSON shape 通过 structured output 强约束，不写进 prompt 正文。** 参考 `TaskWriteExecutor.JsonSchema` 的写法。

```jsonc
{
  "name": "log_chat_modifications",
  "strict": true,
  "schema": {
    "type": "object",
    "properties": {
      "modified_tasks": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "编号":        { "type": "integer" },
            "任务名":      { "type": "string" },
            "周几":        { "type": "string" },
            "持续小时":    { "type": "number" },
            "开始时间":    { "type": "string" },
            "结束时间":    { "type": "string" },
            "类别":        { "type": "string" }
          },
          "required": ["编号","任务名","周几","持续小时","开始时间","结束时间","类别"],
          "additionalProperties": false
        }
      },
      "reason": {
        "type": "string",
        "description": "一句中文说明本次改动理由，用于调试与用户查看。"
      }
    },
    "required": ["modified_tasks","reason"],
    "additionalProperties": false
  }
}
```

- 每条 modified_tasks 必须包含全部 7 个字段，即使某字段未变（未变的字段填原值）。
- `reason` 是整批的一句总结，非 per-task。

### 4.3 可改 / 不可改字段

| 字段 | 可改 | 备注 |
|---|:---:|---|
| 编号 | **不可** | 系统 prompt 里**非常绝对地强调** LLM 不得修改。本地 Step 4 再做一次保险比对。 |
| 任务名 | 可 | |
| 周几 | 可 | |
| 持续小时 | 可 | |
| 开始时间 | 可 | 保持 ISO 8601 带时区格式 |
| 结束时间 | 可 | 同上 |
| 类别 | 可 | 建议 LLM 优先从已存在的类别中选，但不强制 |
| task_group_id / source_session_ids / source_key / session_names | **不发给 LLM** | 保留在本地 items，不进入自然语言列表，也不在 schema 中出现，重建 preview 时原样带回 |

### 4.4 一致性约束（系统 prompt 显式提醒）

系统 prompt 中必须包含：

- **绝对不允许修改"编号"字段**。
- 如果改了 开始时间 / 结束时间 / 持续小时 中的任何一个，必须保证 `结束时间 - 开始时间 == 持续小时`。
- 只返回需要修改的任务，未修改的任务不要放进 `modified_tasks`。
- 每条 modified_tasks 必须完整返回 7 个字段（未变的字段填原值）。

## 5. 一致性违反处理

Step 5 检测规则：

```python
duration_seconds = (parse(结束时间) - parse(开始时间)).total_seconds()
expected_hours = round(duration_seconds / 3600, 2)
if abs(expected_hours - 持续小时) > 0.01:
    # 违反
```

违反时：**不阻断、不自动修正**。行为如下：
- 在该条 diff Panel 顶部加一行红字警告，含 `编号`、`任务名`、`结束-开始=Xh`、`声明持续小时=Yh`。
- 是否落盘完全由用户在该条的 y/n 决定：
  - 用户输 y → 违反数据照原样落盘。
  - 用户输 n → 保留旧条目，违反数据丢弃。

## 6. 兜底重叠检查

Step 8：对最终 items（包含未改动的）两两比较 `[开始时间, 结束时间]`：

- 视为闭区间；只要两条区间有交集就算重叠。
- 有一对或多对重叠时，打印一段"⚠ 时间重叠"信息，每对一行：
  ```
  编号 {A} ({任务名 A}) [开始A ~ 结束A]
    与 编号 {B} ({任务名 B}) [开始B ~ 结束B] 重叠
  ```
- 不阻断、不修改、不要求确认，仅提示。

## 7. 落地位置

- **新增文件**：`chat.py`（不加到 summary.py 因为 summary.py 已经过大）。
- **命令路由**：修改 `guiyuniban_control.py`，添加 `log chat` 分支，参照 `log push` 分支模式。
- **依赖复用**：
  - `_openai_json` / OpenAI 调用逻辑：优先复用 summary.py 里的现成实现；如需要就 import 而非复制。
  - `_extract_commit_payload` / `_build_commit_preview_text` / `read_txt_records` 等：从 summary.py import。
  - rich 渲染模式：参考 summary.py 的 `_render_*` 函数。
- **模型**：沿用 summary.py 的 `DEFAULT_OPENAI_MODEL = "gpt-5.4"`。
- **配置读取**：沿用 summary.py 的 `read_config` + 环境变量优先级。

## 8. UI 规范

### 8.1 Step 3 请求前

打印一个 rich Panel：
- 标题："log chat 请求"
- 内容：模型名 + 用户指令预览 + items 数量。

### 8.2 Step 4 过滤提示

对每条被过滤掉的条目输出一行黄色警告文本。多条时按顺序输出，不合并。

### 8.3 Step 6 逐条 y/n

每条一个独立 rich Panel：
- Panel 标题："变更 {i}/{N}  编号 {X}"
- （可选）红色警告行：一致性违反提示
- 主体表格：
  - 表头：`字段 | 变更前 | 变更后`
  - 只列真正有变化的字段
  - 单元格 `overflow="fold"`、`no_wrap=False`；Panel `expand=False`（不裁剪、内容长时换行）
- Panel 下方：`[y/n] `（`input()` 收输入；非 y/n 视为 n）。

无 rich 时退化为纯文本输出，字段按行打印，输入方式相同。

### 8.4 Step 7 落盘后

绿色 Panel："commit_preview 已更新"，含：接受数、拒绝数、未变数、`generated_at`。

### 8.5 Step 8 重叠提示

如果没有重叠：输出一行 dim 文本"未检测到时间重叠"。
如果有重叠：一个黄色 Panel 列出所有重叠对。

## 9. 边界情况

| 情况 | 行为 |
|---|---|
| 空指令 | 报错退出，返回码 2 |
| commit_preview.txt 不存在或为空 | 报错退出，返回码 1 |
| commit_preview.txt 缺 JSON payload | 报错，提示重跑 log commit |
| LLM 请求失败 / 超时 / 返回非法 JSON | 复用 summary.py 的错误 Panel；退出返回码 1 |
| LLM 返回空 `modified_tasks` | 提示 "LLM 未提出任何修改" 后正常退出，返回码 0 |
| 所有 LLM 返回项都被 Step 4 过滤掉 | 同上，正常退出 |
| 用户对全部条目输 n | 不重写 preview 文件（原文件保持不变）；仍执行 Step 8 重叠检查 |
| 用户中途 Ctrl+C | 不重写 preview 文件，原文件不变 |
| LLM 返回的时间格式不合法 ISO | 视为一致性违反同样通过 y/n 让用户决定；本地不做自动修正 |

## 10. 不做的事（明确排除）

- **不迭代**：一次 LLM 调用完成全部修改建议，无 "LLM 追加 id → 本地补详情" 循环。
- **不改** `uncommit_tasks.txt` 里的原始 session 记录。
- **不改** `task_index.txt` 计数器。
- **不 push** 到 Notion（那是 `log push` 的事）。
- **不重新分配编号**。
- **不做类别有效性校验**（不与 Notion "任务分类"页交叉验证）。
- **不做自动修正**（一致性违反、时间格式错误、字段类型异常统统由用户 y/n 决定是否接受）。
