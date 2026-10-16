# 第六讲：可靠的循环——流式输出、重试、取消与并发

[返回学习路线](README.md) · [上一讲：上下文管理](05-context-management.md)

前五讲把一次 Agent 工作拆成了几层。第二讲说清一次模型请求和响应长什么样，第三讲把工具调用变成可执行、可校验的动作，第四讲把状态搬进只追加的事件日志，第五讲回答每个请求该带多少上下文。到这里，一个「理想情况下能跑通」的循环已经完整。

但真实运行很少理想。模型的回答不是一次性返回的字符串，而是一条分片流；网络可能在中途断开；用户可能按下停止；多个工具调用可以同时跑，也可能必须排队；进程可能在写到一半时被硬中断。一个只会 `for chunk in stream: print(chunk)` 的循环，在这些情况下会同时丢失正确性和可解释性。

这一讲处理的就是这些问题：一次尝试（attempt，指对该步骤的一次模型请求）怎样才算结算完成、失败之后怎样重试、取消怎样传播、并发怎样受限、没有结果的工具调用怎样收尾。配套实验 `examples/reliable_loop.py` 仍然离线运行，只使用标准库——它模拟协议和状态机，不调用任何模型。

## 1. 为什么 `for chunk in stream` 还不够

最朴素的写法是把每个分片直接打印或拼接：

```python
answer = ""
for chunk in stream:
    if chunk.type == "text-delta":
        answer += chunk.text
        print(chunk.text, end="")
```

这段代码在「网络一切正常、只返回一段文本」时看起来是对的，但它无法回答几个关键问题：

- 这条流**真的结束了吗**？连接正常关闭、进程崩溃、服务端中途断开，程序看到的现象可能都是「迭代器不再产出」。
- 这一轮模型请求**算是成功还是失败**？失败要不要重试？重试几次？
- 已经打印给用户的文字，在该尝试被判失败后，要不要撤回？
- 返回的文本里混着推理内容和工具调用，怎么区分？
- 如果这是第二次重试，日志里第一条失败的尝试该记成什么？

这些问题的共同点是：它们都要求把「一段字节流」变成「一个明确的、可以持久记录的结论」。分片只是传输单位，它本身不是结论。第五讲强调过「上下文是一次装配结果」，这里对应的一句话是：

> 一次模型尝试的结论，是一个显式的终止结果，而不是「流自然走完了」。

所以协议里最重要的分片不是文本，而是终止分片 `finish`。它出现，才代表这次尝试有了结论；没出现，就只能当作中断处理。

## 2. 分片协议：从零散增量到一个消息

流式输出的分片是**有结构**的。教学实验只保留真实系统里最关键的一类，命名都沿用 DeepSeek Harness 的 `StreamChunk`：

| 分片 | 作用 |
|---|---|
| `block-start` | 一个内容块开始，声明它的序号和类型（文本、推理、工具调用） |
| `text-delta` | 向某个文本块追加可见文本 |
| `reasoning-delta` | 向某个推理块追加思考内容 |
| `tool-call-delta` | 向某个工具调用块追加调用编号、名称和参数片段 |
| `block-end` | 一个内容块结束，携带它组装完成的完整值 |
| `usage` | 本次请求的 token 用量 |
| `finish` | **终止分片**，声明这次尝试为什么结束 |

这七种分片不是随意起的名字，它们对应两类信息：**正在传输的增量**和**已经确定的结构**。`text-delta` 是增量，`block-end` 是结构；`usage` 是元数据，`finish` 是结论。

组装函数 `assemble_chunks(chunks)` 做的事情，就是把这条流折叠成三样东西：一组内容块（`blocks`）、一份 `usage`、一个终止原因（`finish`）。实验里还额外保留了一个 `replay_state` 字段，它是给适配器私有回放数据留的位置，后文第十讲会用到。

`finish` 不是一个字符串，而是一个带三个字段的记录：

```python
@dataclass(frozen=True)
class FinishReason:
    kind: str          # stop / tool-calls / max-tokens / error / aborted
    code: str | None = None   # 失败时的稳定错误码，例如 RATE_LIMIT
    message: str = ""         # 给人看的失败说明
```

把它拆成 `kind`、`code`、`message` 三个字段，是因为它们回答三个不同的问题：

- `kind` 是**机器**判断的分支：这次是正常说完、要调工具、被长度截断，还是出错；
- `code` 是**策略**判断的依据：`RATE_LIMIT`、`SERVER`、`TIMEOUT` 这些值决定要不要重试；
- `message` 是**人和日志**读的说明，不应该被程序拿去匹配。

常见的 `kind` 有：`stop`（自然结束）、`tool-calls`（模型要求调用工具）、`max-tokens`（被输出长度上限截断）、`error`（请求或流失败）、`aborted`（被取消）。

组装时有两条硬规则。

第一，`finish` 之后不能再有分片。流一旦终止，后续任何分片都说明协议被破坏，实验里直接抛 `CHUNKS_AFTER_FINISH`。真实系统里这句校验的价值在于：它防止一个「迟到的重复分片」被当成新内容追加，破坏已经持久化的结论。

第二，`finish` 必须存在。如果流走完了却没有终止分片，实验抛 `MISSING_TERMINAL_FINISH`。这正是第 1 节说的：自然关闭不等于成功结束。

`usage` 协议上出现在 `finish` 之前。这样终止分片一旦被处理，之前累积的用量就已经完整，不需要在结束时再回头查询。

### 2.1 成功与失败是两种事件，不是同一种事件加个标记

组装出的结论会被写进会话日志。这里有一个容易忽略但很重要的设计：**成功和失败写的是不同事件类型**。

- 成功（`stop`、`tool-calls`、`max-tokens`）写 `assistant/message`；
- 失败写 `assistant/attempt`。

```json
{
  "type": "assistant/message",
  "data": {
    "finish": "tool-calls",
    "blocks": [
      { "type": "text", "text": "我先读取配置文件。" },
      { "type": "tool-call", "id": "call-1", "name": "read", "arguments": "{\"path\":\"package.json\"}" }
    ]
  }
}
```

为什么不是「都写 `assistant/message`，加一个 `ok: false`」？因为这两者在下游的语义完全不同。`assistant/message` 会进入派生历史，成为模型下一次请求能看到的内容；`assistant/attempt` **不进入派生消息**，它只用于说明「这里尝试过、失败了」。派生历史是模型可见的投影，失败尝试、重试延迟、提供方错误这些属于运维信息，不该污染模型上下文。

有一个反直觉但正确的例子：模型返回了**完全为空**的内容，或者以 `max-tokens` 结束——这在协议上仍然算成功模型调用，写 `assistant/message`。只是「空内容的消息」不会作为有意义的条目进入派生历史。换句话说，「模型这次没说出有用的话」和「模型请求失败了」是两件事，日志要用不同事件表达。

## 3. 两种「没结束完」：截断与中断

流可能以两种不同的方式「不完整」，它们的处理方式完全不同，混在一起会出错。

**第一种是服务端给出的 `max-tokens`。** 模型是被自己的输出上限截断的，它可能刚刚开始写一个工具调用，参数只发了一半，比如：

```python
ToolCallDelta(1, "call-1", "edit", '{"path":"a.py","content":"broken')
```

这段参数是非法 JSON，即使强行解析也可能得到一个残缺的编辑动作。执行它是危险的。所以规则是：**`max-tokens` 时丢弃所有工具调用**，只保留截断前已经完整或已经积累的文本、推理内容。实验里 `assemble_chunks` 在终止原因是 `max-tokens` 时跳过工具调用块，回放的持久数据也按同样规则裁剪。这条规则一致性很重要：如果实时显示丢弃了、回放却重建出一个工具调用，同一份历史就会出现两种解释。

注意这里丢的是**整个工具调用**，不是「先执行能解析的部分」。部分工具调用无法保证语义，宁可让模型在下一轮重新发一次，也不要执行一个参数不完整的外部动作。

**第二种是进程被硬中断。** 进程在 `finish` 到达之前就结束了——崩溃、被 `kill`、断电。这时连一个终止分片都没有，组装函数会直接报错。但用户可能已经在屏幕上看到了一部分文字。这些文字要不要保留？

实验用 `interrupted_blocks(chunks)` 回答：**只保留中断前已经出现的、非空的文本和推理内容，不保留工具调用。**

理由有两条。其一，用户确实看见了，撤回会造成「屏幕内容跳变」，所以可见文本应当作为历史留下。其二，工具调用**还没有被安全分发**，把它当成一次完成的调用会要求凭空造一个并不存在的工具结果，所以一律丢弃。推理内容保留是因为它同样是模型产出的、可以解释后续行为的产物，具体怎样呈现由前端决定。

还有一条更细的边界：`interrupted_blocks` 只看单个内容块是否累积到了非空文本。一个只发了 `block-start` 就断掉的空块会被丢弃——空块没有任何信息量。

## 4. 步骤、轮次与协作式取消

在讨论取消之前，先把两个时间尺度说清楚。

- **轮次（turn）**：从用户（或系统）给出一段输入开始，到 Agent 认为这一轮任务告一段落为止。一轮里可能调用模型很多次。
- **步骤（step）**：轮次内部的一次迭代——调用一次模型，执行它要求的工具，再把结果写回历史，准备下一步。

一个简单的判断方法：**步骤以模型请求为界，轮次以任务告一段落为界**。用户说「帮我把这个 bug 修掉」，可能触发一次模型调用（步骤），模型让改文件（步骤内执行工具），再调用一次模型确认（又是一个步骤），整个从收到请求到回话是一次轮次。

取消是**协作式（cooperative）**的。这意味着代码不会因为有人按下停止就被强行从执行中间「掐断」——那种做法会留下写到一半的文件、尚未回滚的副作用和无法解释的日志。协作式的做法是：运行中的代码持有一个取消信号，各处代码在可以安全停下的点检查这个信号，然后主动收尾。

实验里这个信号是 `CancellationToken`：

```python
class CancellationToken:
    def __init__(self, cancelled=False):
        self.cancelled = bool(cancelled)

    def cancel(self):
        self.cancelled = True

    def raise_if_cancelled(self):
        if self.cancelled:
            raise Cancelled()
```

它非常小，但包含了协作式取消的两个要点：**取消是设置状态，不是抛异常打断任意位置**；**真正停下来是因为有人主动检查并抛出**。

在真实系统里，DeepSeek Harness 的 `agent.cancel()` 中止当前活动，默认还会清空待处理输入（inbox）；如果调用时传 `keepInbox: true`，则只中止当前轮次，保留排队等待的用户消息，等这一轮结束后继续处理。这区分了两件事：用户说「这次回答别继续了」和用户说「把整个任务都停掉」，需要的语义并不一样。

取消还有一条重要规则：**已经流式交付给用户的文本会保留。** 理由和第 3 节的硬中断一致——用户已经看见的内容不能凭空消失。这也是为什么取消必须走协作式路径、在能安全收尾的位置处理，而不能粗暴丢弃缓冲区。

## 5. 重试：先持久、后等待，发生在打开的步骤边界

网络抖动、限流和服务端错误都会导致一次模型请求失败。最自然的想法是包一层重试：

```python
for attempt in range(3):
    try:
        return llm.stream(request)
    except TransientError:
        time.sleep(delay)
```

对**原始流**做这种包装，在持久化系统里是错的。原因是：原始流无法可靠地区分「哪些分片已经交付出去了」。如果第一次尝试已经发出了一部分文本，包装层重试，就可能出现重复文本、重复工具调用，或者用户看到的内容和历史对不上。

DeepSeek Harness 的做法是：**重试发生在持久 agent 步骤的边界上，而不是包住 `ctx.llm.stream()`。** 一个步骤是打开的（open step）——失败步骤还没有被写成确定的结论，会话历史还停在这次步骤开始之前的状态。重试就是「把同一个打开的步骤重跑一遍」，基于同一份持久历史重建请求。直接调用 `ctx.llm.stream()` 的消费方不享受重试，因为一个原始流无法持久地区分已发出的分片。

失败之后是否重试，由提供方路由的 `retryPolicy` 决定，有两种模式。

| 模式 | 行为 | 终止条件 |
|---|---|---|
| `normal` | 只重试配置里列为瞬时故障的错误码，重试次数有上限 | 成功、取消、或预算/错误码不符合 |
| `always` | 重试每一个模型请求失败，没有尝试次数上限 | 成功、取消、或插件释放 |

`normal` 模式的默认值是：对 `EMPTY_RESPONSE`、`RATE_LIMIT`、`SERVER`、`TIMEOUT`、`TRANSPORT` 最多重试五次；退避从 500 毫秒起步，封顶 10 秒，带 10% 的对称抖动。抖动的作用是：当很多客户端同时失败时，避免它们在完全相同的时刻一起重试、再次把服务打垮。

实验把退避写成一个确定性的函数，方便测试：

```python
def retry_delay_ms(policy, attempt, random_unit=0.5):
    base = min(policy.max_delay_ms, policy.initial_delay_ms * (2 ** max(0, attempt - 1)))
    factor = 1 + (random_unit * 2 - 1) * policy.jitter_ratio
    return max(1, int(base * factor))
```

`attempt` 从 1 开始，`base` 是指数增长并封顶的延迟，`random_unit` 取 0.5 时抖动被抵消，因此测试可以断言精确数值；取 0 或 1 时得到下界和上界。真实实现用带最大值的定时器延迟，并提供方返回的、在边界内的 `Retry-After` 会替换本地退避。

重试还有一个顺序上的硬规则：**先持久，后等待。** 每次计划中的重试，在任何等待开始之前，就先写一条非表层事件 `llm/retry`；等待完成后、真正开始重试之前，再写 `llm/retry-started`。

```json
{
  "type": "llm/retry",
  "data": {
    "attempt": 1,
    "mode": "normal",
    "code": "SERVER",
    "delayMs": 500
  }
}
```

这两个事件都**不对模型可见**，不进入派生历史。它们存在的意义是防止一类难以调试的状态：如果程序在退避等待期间崩溃，日志里找不到「本来打算重试」的记录，恢复时就会误以为这一轮已经结束，或者不知道还有一次重试在等。先写 `llm/retry`，就把「打算重试」变成了持久事实；恢复过程因此可以从日志重建这次重试计划。

顺序上的另一面是取消。取消或插件释放会**中止进行中的退避**，并且因为 `llm/retry` 已经先写、`llm/retry-started` 还没写，日志能清楚区分「已经计划但没开始」和「已经开始」两种状态。实验里的 `test_cancellation_during_backoff_stops_before_retry_started` 就断言了：取消之后事件序列停在 `assistant/attempt` + `llm/retry`，不会多出 `llm/retry-started`。

最后一条设计约束是：重试**不会重复已经确定完成的工具调用**。因为重试是在打开的步骤边界重跑，而一个步骤里「已经确定完成的工具结果」是持久事实；重试只重跑失败的那次模型请求及其尚未确定的部分，不会把已经落地的工具动作再做一遍。

## 6. 并发工具调用与独占屏障

一次模型响应可能同时请求多个工具调用。有些可以安全地同时跑，比如读三个不同的文件；有些必须独占，比如写文件和另一个写同一个文件、或与它顺序相关的读。DeepSeek Harness 用两个概念表达这件事。

**并行上限。** 配置项 `maxParallelToolCalls` 默认是 10，表示一个步骤里同时在途的「并行安全」调用最多有多少个；设成 1 就退化成串行。它只在下一个工具组开始时读取，因此运行中调整它不会让正在进行的组突然改变并发度。

**独占屏障。** 被标记为独占（exclusive）的调用必须单独运行，并且对**前后两侧**都构成排序屏障：它要等前面所有并行安全调用结束，它执行期间没有别的调用，它结束之后后面的调用才能开始。这正是「读—改—读」这类序列需要的语义。

实验用一个纯数学的调度模型表达这套规则，不真的开线程，因此结果完全确定、可在测试里精确断言：

```python
def schedule_tool_calls(calls, max_parallel=10):
    lanes = [0.0] * max_parallel
    scheduled = []
    for call in calls:
        if call.execution_mode == "exclusive":
            start = max(lanes)
            end = start + call.duration
            lanes = [end] * max_parallel     # 屏障：把所有泳道推到它结束
        else:  # parallel-safe
            lane = min(range(max_parallel), key=lambda i: (lanes[i], i))
            start = lanes[lane]
            end = start + call.duration
            lanes[lane] = end
        scheduled.append(ScheduledToolCall(call.call_id, start, end))
    return tuple(scheduled)
```

把每个并行安全调用放进「最早空闲的泳道」，独占调用则把自己的结束时间**写满所有泳道**。用 `max_observed_concurrency` 统计任意时刻的重叠数，就能验证峰值不超过上限、独占调用前后都没有重叠。

真实系统的分类是**二元**的：一个工具要么并行安全，要么独占，没有第三档。判断依据不是工具名字，而是它的安全性：比较或修改共享状态的调用必须独占。这个判断由工具作者和评审共同保证，而不是运行时猜测——运行时没有足够信息知道两个 shell 命令会不会改同一个文件。

## 7. 未完成工具调用的收尾

到这一节，前面几节的机制会汇成一个最容易出错的问题：**一个工具调用已经被发起，但结果没有出现。** 这时历史里留下一个没有配对的 call，模型下一次请求会看到非法的工具协议。必须给每个没有结果的调用补一个结果，并且补的措辞要能指导正确的重试决策。

实验把它们分成三类，区分的依据是「有没有分发（dispatch）」和「有没有启动记录」：

| 情况 | 判定依据 | 错误码 | 对模型和调用方的含义 |
|---|---|---|---|
| 取消后未分发的调用 | 取消阻止了分发 | `ABORTED_BEFORE_DISPATCH` | 确定没有执行，安全，需要时可以重试 |
| 已启动但无结果 | 有启动记录，但没有结果 | `UNKNOWN_OUTCOME` | 结果未知，只有只读或幂等操作可以重试 |
| 中断前尚未开始记录 | 既无分发也无启动记录 | `INTERRUPTED_BEFORE_START` | 尚未开始，需要时可以重试 |

这三类的差别不是文字游戏，而是**副作用风险**的差别。

- `ABORTED_BEFORE_DISPATCH` 说的是「因为取消，这次分发被阻止了」，结果文本是固定的 `Error: tool call aborted before dispatch`。它确定没有产生任何副作用。
- `UNKNOWN_OUTCOME` 说的是「工具已经开始，但结果未知」。文本会明确提示：只有只读或幂等操作可以重试，否则必须先核验外部状态或询问用户。想象一次「发送邮件」或「创建订单」调用——结果未知时贸然重试可能产生重复副作用，所以措辞必须阻止这种重试。
- `INTERRUPTED_BEFORE_START` 说的是「中断前还没有记录为已启动」，因此可以安全地按需重试。

```python
def reconcile_unfinished_tools(calls, started_ids, results, aborted_ids=()):
    reconciled = dict(results)
    for call in calls:
        if call.call_id in reconciled:
            continue
        if call.call_id in aborted:
            reconciled[call.call_id] = {"is_error": True, "code": "ABORTED_BEFORE_DISPATCH", ...}
        elif call.call_id in started:
            reconciled[call.call_id] = {"is_error": True, "code": "UNKNOWN_OUTCOME", ...}
        else:
            reconciled[call.call_id] = {"is_error": True, "code": "INTERRUPTED_BEFORE_START", ...}
    return reconciled
```

注意 `reconcile_unfinished_tools` 的第一条规则是**不覆盖已有结果**。收尾只处理「没有结果」的调用；一个已经落地的成功或失败结果不能被推断出的合成结果替换，否则就等于篡改已经发生的事实。实验里的 `test_completed_results_are_not_overwritten` 专门断言了这一点。

这三类结果会被写进历史，作为下一次模型请求的一部分。它们**是追加**的，位于可复用前缀之后，因此不会使已有的 KV Cache 失效——这一点第 8 节还会用到。

## 8. 流式输出与持久历史、KV Cache 的关系

把前面几节的数据流关系收拢一下，会看到三条明确的边界。

**第一，实时分片和持久结算不是同一份数据。** `agent/assistant-stream` 是把分片实时推给前端的瞬态通道；回放读取的是持久结算（`assistant/message` 或 `assistant/attempt`）。这种分离是刻意的：前端需要逐字出现的体验，恢复和历史需要稳定、可重建的结论。如果回放直接读瞬态分片，任何一次进程重启都会让历史对不齐。

**第二，结算之前的硬中断不会留下持久的 attempt stream。** 因为结算还没写出，崩溃发生在哪个分片、发出了多少个字符，都不构成会话日志里的事实。恢复时只能依据第 3 节的规则，从「用户确实看见过的可见文本」出发重建，而不是假装有一个完整的流。

**第三，重试和恢复都是追加。** 重试基于同一份持久历史重建请求，因此重建出来的请求前缀与之前一致，可以命中提供方的前缀缓存（KV Cache）；`llm/retry`、`llm/retry-started`、恢复结果都是非表层或追加事件，不改动已有前缀。相反，**表层替换**（比如第五讲的压缩把旧前缀换成摘要）会从第一个被替换的位置起使缓存失效。理解这条边界，就能判断一个改动是「便宜地追加」还是「昂贵地重写前缀」。

## 9. 对照 DeepSeek Harness 源码

本讲涉及的事实可以在固定版本里逐条核对。源码链接都指向本轮学习使用的提交 `5badb150`：

- [agent-loop 包说明](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/core/agent-loop/README.zh.md)
- [agent 包说明（创建、取消、inbox）](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/core/agent/README.zh.md)
- [Agent 生命周期](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/agent-lifecycle.zh.md)
- [LLM 流式子系统：StreamChunk 协议](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/subsystems/llm-streaming.zh.md)
- [llm 包说明](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm/README.zh.md)
- [llm-retry 执行器说明](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-retry/README.zh.md)
- [重试策略与默认值：retry-policy.ts](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm/src/retry-policy.ts)
- [流组装与 interruptedBlocks：assembler.ts](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm/src/assembler.ts)

重点看：

- `finish` 是唯一的终止信号，`usage` 在它之前；
- `assembleChunks` 与 `interruptedBlocks` 对 `max-tokens` 和硬中断的不同处理；
- `assistant/message` 与 `assistant/attempt` 的分工；
- `maxParallelToolCalls` 的读取时机与独占屏障；
- `llm/retry` 在 `agent/request-error` waterfall 上的位置，以及「先持久、后等待」；
- 三类未完成工具结果的措辞，尤其是「结果未知」时对重试的限制。

## 10. 思考与练习

1. 为什么「流自然走完了」不能当作成功结束？缺了终止分片时，程序应该怎么做？
2. 为什么一次尝试的成功和失败要写成两种事件类型，而不是一种事件加一个布尔字段？
3. `max-tokens` 时为什么丢弃整个工具调用，而不是尝试执行参数已经收到的部分？
4. 硬中断后，为什么保留可见文本、却丢弃工具调用？两者的依据有什么不同？
5. 为什么重试要发生在持久步骤边界，而不是包在 `ctx.llm.stream()` 外面？
6. `llm/retry` 为什么要在等待之前写，而不是等待之后再写？先写能防止哪一类恢复问题？
7. 独占工具调用的「双向屏障」分别保护了前侧和后侧的什么场景？
8. 同样没有结果的工具调用，为什么要区分「取消未分发」「结果未知」「尚未开始」三种？
9. 哪些改动是「廉价追加」、哪些是「昂贵地重写前缀」？这对 KV Cache 意味着什么？

<details>
<summary>自查要点</summary>

1. 连接关闭、崩溃和正常结束在流的层面可能无法区分。协议用终止分片作为唯一权威；没有它就只能当失败或中断，不能当成功。
2. 成功消息进入派生历史、对模型可见；失败尝试不进入派生消息，只是运维记录。用类型区分，才能让「模型能看见的东西」和「只写日志的东西」天然分开。
3. 参数可能是不完整或非法的 JSON，执行它会产生不可预期甚至危险的动作。宁可让模型下一轮重新发一次，也不要执行一个参数不完整的工具调用。
4. 可见文本的依据是「用户确实看见了，撤回会造成跳变」；工具调用的依据是「它还没被安全分发，保留下来就要凭空编造一个结果」。两条依据不同，结论可以相反。
5. 原始流无法持久地区分哪些分片已经交付。包在流外面重试，可能产生重复文本或重复工具调用。步骤边界有持久历史作为锚点，重跑是可重建的。
6. 先写 `llm/retry` 把「打算重试」变成持久事实。如果等待期间崩溃，恢复可以知道还有一次计划中的重试，而不是误判轮次已结束或状态丢失。
7. 前侧屏障保证独占调用在它前面所有并行安全调用结束之后才开始；后侧屏障保证它结束之前，后面的调用不开始。两者共同维护「读—改—读」这类序列的正确顺序。
8. 因为副作用风险不同。取消未分发和尚未开始可以安全重试；已经启动但结果未知的调用，重试可能造成重复副作用，所以只允许只读或幂等操作重试，否则先核验外部状态。
9. 普通追加（新消息、重试事件、恢复结果）位于已有前缀之后，命中缓存；表层替换（压缩等）从第一个被替换的位置起使缓存失效。判断标准是「已有前缀有没有被改动」。

</details>

## 11. 当前实现范围

本讲的离线实验 `examples/reliable_loop.py` 演示：

- 七种流分片如何组装成内容块、用量和终止原因；
- `finish` 是唯一终止信号，且之后不允许再有分片；
- `max-tokens` 丢弃被截断的工具调用；
- 硬中断只保留可见文本和推理，不保留工具调用；
- `CancellationToken` 表达协作式取消；
- 指数退避、封顶与对称抖动，以及「先写 `llm/retry`、后等待」的顺序；
- `normal` 与 `always` 两种重试模式；
- 并行安全的泳道调度与独占调用的双向屏障；
- 三类未完成工具调用的收尾结果，且不覆盖已有结果。

它没有实现真实网络请求、真实定时器、真实线程或进程并发、提供方 `Retry-After` 解析、真实 KV Cache、真实会话日志持久化或在线模型调用。实验里的调度是纯数学的确定模型，用来说明协议约束，不测量真实并发性能。

2026-10-16 已在 Windows、Python 3.13.2 下运行本讲示例，并通过 17 项行为测试。文档中的 JSON 示例和本地章节链接也经过检查。本轮未运行在线模型，也未启动 DeepSeek Harness。

下一讲进入 Cordis 插件基础：Context 与服务容器、依赖注入、事件分发、可逆副作用，以及插件怎样卸载和重新加载。

[返回学习路线](README.md) · [上一讲](05-context-management.md)