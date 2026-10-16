# 第四讲：会话日志、状态重建与崩溃恢复

[返回学习路线](README.md) · [上一讲：工具执行器](03-tool-executor.md)

前三讲已经把最小循环、真实模型 API 和工具执行器搭起来了。但它们的运行状态都在内存里：程序一退出，历史就消失；工具动作执行到一半崩溃，重启后也不知道它到底有没有发生。

这一讲从“为什么不能只保存聊天数组”开始，逐步引入只追加的会话日志、消息投影、检查点和崩溃修复。配套实验仍是离线运行，不调用模型，也不真正写文件。

## 1. 聊天数组看起来已经包含了历史

最直接的状态保存方式是维护一个消息列表：

```python
history = [system_message, user_message]

response = call_model(history, tools)
history.append(response)

for call in response.tool_calls:
    result = execute_tool(call)
    history.append(tool_result_message(call.id, result))

response = call_model(history, tools)
```

运行上面这段代码时，history 确实能让模型看见之前的消息。但它把四种不同的事情压在同一个数组里：

| 数组中的内容 | 它实际回答的问题 |
|---|---|
| 用户消息 | 用户要求了什么 |
| assistant 消息 | 模型说了什么、请求了什么动作 |
| tool 结果 | 程序观察到了什么 |
| 调用参数 | 动作应该怎样执行 |

当任务顺利结束，这个数组已经够用了。问题出在需要“往回看”和“从中恢复”的时候。

- 数组被不断追加和修改，早期状态没有留下。
- 模型看到的文字和程序真正执行的动作没有分开记录：显示层重放历史时，不知道哪一条消息触发了哪一次工具调用。
- 一次工具调用可能已经产生了外部副作用，但结果还没写回数组。此时程序崩溃，内存和历史一起消失。
- 如果直接把历史里的工具调用重新执行一遍，写文件、发请求、提交订单这类操作可能被执行两次。
- 历史增长后需要压缩、替换或截断，但原始记录已经被覆盖，无法审计。

结论是：聊天数组适合作为“给模型的输入”，不适合作为 Agent 状态的唯一真源。

## 2. 只追加的事件日志

把状态改成事件列表。每个事件表示“在某个时刻发生了什么事”，而不是“当前聊天记录长什么样”。

一个事件包含四个字段：

```json
{
  "seq": 5,
  "time": 1700000000000,
  "type": "tool/call",
  "data": {
    "turn": 0,
    "step": 0,
    "call_id": "call_1",
    "name": "read_file",
    "arguments": "{\"path\":\"package.json\"}"
  }
}
```

- seq 是会话内单调递增的序号，通常就等于当前日志长度。
- time 是事件时间，用于排序和诊断。
- type 说明事件种类，例如 turn/start、user/message、assistant/message、tool/call、tool/result。
- data 是这一类事件的负载，必须能序列化成 JSON。

日志只有两个基本操作：

```text
append(event)   在末尾记录一个新事件
read(range)     按顺序读取已有事件
```

已经写入的事件不再修改，也不删除。这个性质叫 append-only（只追加）。它带来几个直接好处：

- seq 天然连续，读取时能发现缺失。
- 任意时刻的前缀都是一份完整的历史快照。
- 恢复时只需要找到最后一个完整前缀，不需要猜测中间状态。
- 压缩或摘要可以追加为新的替换事件，而不是改写旧事件。

一次正常回合的事件顺序大致是：

```text
turn/start
  step/start
    system/message
    user/message
    assistant/message
    tool/call
    tool/result
  step/end
turn/end
```

其中 turn 是一轮用户请求，step 是一次模型调用加上它请求的工具执行。边界事件不进入模型消息，但它们让恢复程序知道哪一步还没有结束。

## 3. 模型历史是投影，不是日志本身

日志里有工具调用、边界事件和诊断信息，模型并不需要全部看到。我们从日志中筛选出“会产生模型消息”的事件，按 seq 顺序投影成消息数组。

配套实验把这几类事件标记为面向模型的事件：

```python
SURFACE_TYPES = frozenset({
    "system/message",
    "user/message",
    "assistant/message",
    "tool/result",
})
```

投影规则是：

| 事件 | 投影出的消息 |
|---|---|
| system/message | system 角色 |
| user/message | user 角色 |
| assistant/message | assistant 角色，包含文字和工具调用请求 |
| tool/result | tool 角色，通过 tool_call_id 与调用配对 |

tool/call 事件是日志的一部分，但不单独投影成消息，因为 assistant/message 里已经包含了模型请求的工具调用。turn/start、step/start 这类边界事件同样不直接进入模型输入。

例如日志中依次出现 system/message、user/message、assistant/message 和 tool/result 后，投影结果是：

```json
[
  {"role": "system", "content": "You are a careful coding agent."},
  {"role": "user", "content": "Read package.json and tell me the version."},
  {
    "role": "assistant",
    "content": "I will read the file.",
    "tool_calls": [
      {"id": "call_1", "name": "read_file", "arguments": "{\"path\":\"package.json\"}"}
    ]
  },
  {
    "role": "tool",
    "tool_call_id": "call_1",
    "content": "{\"name\":\"learning-demo\",\"version\":\"0.1.0\"}",
    "is_error": false
  }
]
```

这个投影有一个重要性质：每次请求模型之前，只要把所有事件按顺序投影一遍，就能得到完整历史。日志是唯一真源，消息数组只是它的一种视图。

压缩和摘要也不会破坏这个性质。它们可以追加一条替换事件，声明“用这段摘要替换前面这一段节点”，投影时按新的事件重新计算。原始事件仍在日志里，可以审计和回放。

## 4. 一次工具调用有多个崩溃点

现在把注意力放到最容易出问题的地方：工具调用。

一次工具调用在时间上至少经过这些点：

```text
A. 模型返回 assistant/message，其中包含一次工具调用请求
B. 程序记录 tool/call 事件
C. 工具正文开始执行
D. 工具正文返回结果
E. 程序记录 tool/result 事件
F. 结果和后续步骤被持久保存
```

如果进程在 A 到 F 之间任意一点崩溃，重启后能知道什么？

| 崩溃位置 | 持久日志里有什么 | 恢复时的判断 |
|---|---|---|
| A 之后、B 之前 | assistant 请求，调用尚未开始 | 调用没有开始，可以按需重试 |
| B 之后、C 之前 | 调用已记录，正文未运行 | 框架无法证明正文没运行，按结果未知处理 |
| C 到 D 之间 | 调用已记录，正文可能执行了一部分 | 结果未知，不能盲目重试 |
| D 之后、E 之前 | 正文已返回，结果尚未记录 | 结果未知，不能盲目重试 |
| E 之后、F 之前 | 结果出现在内存，但尚未持久 | 以持久日志为准，仍视为结果未知 |
| F 之后 | 调用和结果都已记录 | 可以继续下一步 |

关键点在于：B 到 F 之间，程序无法从“有没有结果”推断“动作有没有发生”。一个写文件操作可能已经写了一半，一个网络请求可能已经在服务端生效，一个消息可能已经发送出去。恢复程序看不见进程内的局部状态，只能依赖最后一条持久记录。

所以，恢复策略必须对这一段保持保守：**没有持久结果，就要把它当作结果未知，而不是重放。** 表中的 A 到 B 一行默认检查点策略正常工作，即正文不会在 tool/call 持久化之前开始。

## 5. 检查点：把“已记录”变成“已持久”

只把事件追加到内存还不够。要让前面的判断成立，必须规定在哪些时刻把内存中的事件刷新到持久存储，并在刷新完成前阻止可能产生副作用的动作。

Harness 的 checkpoint policy 定义了三个屏障：

```text
1. 模型请求在适配器流构造之前 flush
2. 顶层工具调用在工具正文运行之前 flush
3. 每个 agent/pre-step 边界 flush
```

对应到本讲的例子：

- assistant/message 被记录并刷新后，才允许执行它请求的工具。
- tool/call 被记录并刷新后，才允许进入工具正文。
- 工具结果和下一步请求在开始下一步之前刷新。

刷新失败时采用 fail-closed（失败即阻止）原则：如果持久写入没有成功，适配器或工具正文就不运行，而不是先执行、后补日志。

为什么第二个屏障必须放在工具正文之前？因为事后再记录已经太晚。假如先执行写文件、再记录 tool/call，那么写入成功但记录失败时，崩溃恢复会看到一条 assistant 消息请求调用，却没有工具调用记录，从而误判为“没有开始”。先记录调用再执行正文，最坏情况只是结果未知；先执行正文再记录，最坏情况是框架对已经发生的副作用一无所知。`n`n检查点覆盖的是已经形成事件的工作。未完成的 assistant 流保持瞬态：如果进程在流结束并形成 assistant/message 或 assistant/attempt 之前硬中断，日志里不会留下半截的流式响应。持久性保证从已经提交的事件开始，而不是试图保存每一个增量 token。

同样地，结果记录得越早越好。工具正文返回后应立即追加 tool/result，并在下一个请求前刷新。所谓“立即”指的是写入顺序，不一定要求每个结果都单独同步磁盘；后端可以批量写入，但检查点必须保证在关键动作前后给出明确的持久性边界。

一个实际后端通常还要处理：

- 写入被缓冲时，flush 必须等待它真正到达存储。
- 进程在写入过程中崩溃时，磁盘上可能留下不完整的尾行。
- 已经完整写入的前缀必须保留，不能因为尾部损坏而丢弃整个会话。

本讲的 DurableLog 用一个内存行列表模拟这些性质。每次 checkpoint 只能把前缀变长；如果新写入试图改写已提交的前缀，就抛出 PREFIX_REWRITTEN。

## 6. 恢复：补齐尾巴，但不重放

启动时，恢复程序读取持久日志，逐事件扫描，找出最后一个尚未闭合的 turn。然后追加两类“收尾事件”：

1. 为没有结果的工具调用补一条错误结果。
2. 如果 step 和 turn 还开着，补上 step/end 和 turn/end。

补结果不是为了假装工具成功了，而是为了让记录重新平衡。模型消息协议通常要求每个 assistant 工具调用后面都有对应结果；如果直接在未闭合的记录上发起下一次请求，供应商会拒绝这条不完整的历史。

收尾结果分成两种错误码：

| 错误码 | 出现条件 | 模型看到的含义 |
|---|---|---|
| TOOL_NOT_STARTED | assistant 请求了调用，但日志里没有 tool/call | 调用没有开始，需要时可以重试 |
| TOOL_OUTCOME_UNKNOWN | 有 tool/call，但没有持久 tool/result | 调用已记录，结果未知，不能盲目重试 |

TOOL_OUTCOME_UNKNOWN 的措辞会明确要求：

- 只读或幂等的操作可以重试。
- 可能有副作用的操作，先核实外部状态或询问用户。
- 不要盲目重试。

这里有一个容易被忽略的细节：即使工具正文还没运行，只要 tool/call 已经持久化，恢复程序也会报告结果未知。因为它无法证明正文没有运行，而“误报未知”比“误报未开始”安全得多。前者可能多一次确认，后者可能重复执行副作用。

恢复事件本身也遵循只追加原则。它们是在日志末尾追加的新事件，带确定性的 seq 和 timestamp，不会修改原有事件。恢复完成后，再执行一次修复应该是幂等的：日志已经平衡，不再产生新的收尾事件。

配套实验的修复函数可以概括为：

```python
def recover(durable):
    events = durable.replay()
    closers = interrupted_turn_closers(events)
    log = SessionLog(events)
    for closer in closers:
        log.append(closer["type"], closer["data"], time=closer["time"])
    if closers:
        log.checkpoint(durable)
    return log, closers
```

它只读取日志、计算收尾事件、追加并刷新，从头到尾没有调用任何工具函数。

## 7. 对照 DeepSeek Harness 源码

### 7.1 Session.append：事件日志的核心入口

`packages/core/session/src/index.ts` 中的 `Session.append()` 负责：

- 把 data 转换成不可变的 JSON 快照；
- 分配 seq 和 time；
- 校验 surfaceOp 和 sourceEventSeqs；
- 把事件追加到内存日志；
- 通过 session/event 通知观察者。

注意它不直接写磁盘。持久化是插件关注点：后端监听 session/event，并在 session/flush 时把缓冲事件写入存储。这个分层让会话核心不依赖具体存储格式。

### 7.2 deriveMessages：从 surface 投影消息

同一个文件中的 `deriveMessages()` 遍历 surface 节点，把消息事件投影为模型历史。surface 是派生历史的唯一来源：tool/call 这类日志事件没有 surfaceOp，不会进入投影；压缩使用 replace 操作替换一段 surface 节点，而原始事件仍然保留。

### 7.3 SessionEventMap：事件类型

`packages/core/session/src/types.ts` 定义了事件类型。与本讲直接相关的有：

- turn/start、turn/end
- step/start、step/end
- system/message、user/message、assistant/message
- assistant/attempt（失败或重试的尝试，不产生模型可见消息）
- tool/call、tool/result
- request/header、request/context
- session/end-seed

每个事件都带 seq、time 和 JSON 可序列化的 data。消息产生事件额外带 surfaceOp，声明它是追加还是替换。

### 7.4 repair.ts：TOOL_NOT_STARTED 与 TOOL_OUTCOME_UNKNOWN

`packages/core/session/src/repair.ts` 里可以看到：

- 两个错误码常量；
- `ToolCallRecovery` 跟踪未回答的工具请求；
- `interruptedTurnClosers()` 扫描未闭合的 turn，生成错误结果、step/end 和 turn/end；
- 收尾事件复用最后一条真实事件的时间，seq 继续递增；
- 结果文本明确区分“没有开始”和“结果未知”。

它也用于 fork：从一条尚未闭合的会话切分支时，分支需要用自己的原因补充收尾事件，避免继承一个悬空的工具调用。

### 7.5 checkpoint policy：三个屏障

`packages/session/session-checkpoint-policy/README.zh.md` 把持久性讲得很清楚：

- 模型请求在适配器流构造前 flush；
- 顶层工具调用在工具正文运行前 flush；
- 每个 agent/pre-step 边界 flush；
- 检查点失败按失败即阻止处理；
- 中断的工具调用以未知结果恢复，不会自动重试。

它同时说明了边界：策略保证的是“执行意图已经被持久记录”，不是“副作用恰好执行一次”。真正需要恰好一次语义的工具，要把调用编号作为幂等键传给外部系统，由外部系统去重。

### 7.6 JSONL 持久化：不可变代际

`packages/session/session-persistence-jsonl/README.zh.md` 描述了磁盘布局：

- 每个会话一个目录；
- 当前格式每行一个事件；
- 默认用 Zstandard 帧存储，也可以关闭压缩；
- 只追加和 fsync，已提交事件不重写；
- 崩溃后保留已完整提交的前缀，丢弃撕裂的尾行；
- 历史格式保留为不可变的 generation 文件，源保持逐字节不变，并通过相邻迁移边升级；写打开以不覆盖方式发布后继，后端没有删除会话文件的接口。

这些细节解释了为什么“只追加”不只是内存里的事：磁盘上的写入路径、尾行恢复和格式演进服从同一套前缀不变原则。

### 固定版本源码入口

- [会话服务与 deriveMessages](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/core/session/src/index.ts)
- [会话事件类型](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/core/session/src/types.ts)
- [崩溃修复与两种错误码](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/core/session/src/repair.ts)
- [surface 投影实现](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/core/session/src/surface.ts)
- [三个持久性检查点](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/session/session-checkpoint-policy/README.zh.md)
- [JSONL 持久化后端](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/session/session-persistence-jsonl/README.zh.md)

## 8. 思考与练习

1. 为什么“模型看到的聊天记录”不能直接当作 Agent 状态的唯一真源？
2. tool/call 已经持久记录，但 tool/result 没有，恢复时为什么不能直接重新执行这次调用？
3. 如果崩溃发生在工具正文返回之后、结果写入之前，恢复程序能确定副作用没有发生吗？
4. 为什么 assistant/message 里的工具调用请求和 tool/call 事件要分开记录？
5. TOOL_NOT_STARTED 与 TOOL_OUTCOME_UNKNOWN 的区别是什么？各自允许什么样的后续动作？
6. 为什么恢复事件也要追加到日志，而不是直接修改内存里的历史？
7. 只读工具和写文件工具在恢复策略上有什么不同？

<details>
<summary>自查要点</summary>

1. 聊天数组是派生视图，丢失了事件种类、调用配对、持久边界和原始历史；压缩或修改后无法审计，崩溃恢复也无法判断副作用状态。
2. 没有持久结果时无法证明调用没有完成。写文件、发请求等操作可能已经生效，重试可能造成重复副作用。
3. 不能。正文已经返回但结果没有持久记录，外部副作用可能已经发生。恢复只能报告结果未知，要求核实或询问用户。
4. assistant/message 描述模型请求了什么，tool/call 描述程序已经记录并将要执行哪一次调用。两者分开后，恢复才能区分“请求了但没开始”和“已记录但结果未知”。
5. TOOL_NOT_STARTED 表示没有持久调用记录，调用可以按需重试；TOOL_OUTCOME_UNKNOWN 表示已有调用记录但结果未知，只读或幂等操作可以考虑重试，可能有副作用的操作必须先核实状态。
6. 追加保留完整历史，让恢复操作本身也可审计、可重复；直接改内存会破坏前缀不变性，下一次恢复可能得到不同结果。
7. 只读操作重试通常不会改变外部状态，风险较低；写操作可能已经部分或全部生效，必须避免盲目重试，优先核实状态或要求用户确认。

</details>

## 9. 当前实现范围

本讲的离线实验 `examples/session_log.py` 演示：

- 只追加事件日志与连续 seq；
- 从事件日志投影模型消息；
- 内存持久前缀与不可改写检查；
- 三类崩溃点：调用检查点之前、调用检查点之后、结果写入之前；
- TOOL_NOT_STARTED 和 TOOL_OUTCOME_UNKNOWN 两种恢复结果；
- 恢复不调用工具正文，重复修复保持幂等。

它没有实现真实磁盘存储、fsync、并发写入、Zstandard 压缩、格式迁移、surface replace、会话 fork 或真正的模型请求。DurableLog 只是一个内存中的持久性模型，用来让顺序和状态可见。

2026-10-16 已在 Windows、Python 3.13.2 下运行本讲示例，并通过 11 项行为测试。文档中的 JSON 示例和本地章节链接也经过检查。本轮未运行在线模型，也未启动 DeepSeek Harness。

下一讲将处理信息越来越多时的上下文问题：工作区指令、技能、上下文选择和压缩为什么会改变模型看到的内容。

[返回学习路线](README.md) · [上一讲](03-tool-executor.md)
