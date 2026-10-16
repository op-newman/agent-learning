# 第九讲：多 Agent 与工作流——委派、协作与收尾

[返回学习路线](README.md) · [上一讲：Harness 装配](08-harness-assembly.md)

前八讲里，我们始终假设「一个会话由一个模型循环驱动」：模型思考、调用工具、拿到结果、再思考，直到结束。这个假设在很多时候是对的，但一旦任务变大，它就开始露出两个破绽。第一，一个循环只有一个上下文窗口，把「读五十个文件」「改三个模块」「写一份报告」全塞进同一段对话，早期的关键信息会被压缩甚至丢掉。第二，一个循环是串行的，凡是能同时做的事都得排队。

真实系统对此的答案是**把工作分出去**：让一个会话变成任务的组织者，把一部分工作交给子代理（subagent）去做，把互相独立的步骤并行启动，再用一块共享的看板记录谁在做什么。DeepSeek Harness 为此提供了三个互相咬合的接口：`ctx.subagents`（委派）、`ctx.agentTeams`（协作）和 `ctx.workflowEngine`（编排）。

这一讲回答四个问题：什么时候值得委派；一个子代理从被创建到拿到结果，中间要经过哪些检查和记账；多个代理共享一块任务板时，如何避免「两个人改同一行」；以及工作流脚本的失败为什么必须收敛成一个封闭的结局，而不是让调用方去猜。

配套实验 `examples/agent_team.py` 用纯 Python 实现了一个离线模拟器，把上面三层各实现了一遍：具名提供方注册表与能力预检、带 revision 的任务板、能跑 `agent()`／`parallel()`／`pipeline()` 的工作流引擎。它不联网、不启动真实子进程，也不调用任何模型。

## 1. 为什么要委派：三种不同的需求

「把任务交给子代理」听起来像一件事，其实至少混着三种不同的动机，想清楚哪一种，才知道该用哪种机制。

**第一种是隔离上下文。** 主代理花在「搜索并阅读二十个文件」上的上下文，对最终结论几乎没有价值——它只需要那句「迁移函数定义在 `src/storage/migrate.ts` 第 42 行」。把这段探索交给子代理，子代理在自己的上下文里读文件、试错、走弯路，最后只把结论交回来。主代理的窗口里留下的是一条干净的事实，而不是二十份文件内容。这一点和第五讲的压缩目标是同一个：**让重要的东西留在窗口里，让过程留在别处。**

**第二种是并行。** 三份互不依赖的调研、三个不同模块的修改，可以在同一段时间里推进。注意这里的「并行」需要主代理自己收敛：子代理各自返回结果，最终是否冲突、要不要重试，仍由主代理决定。

**第三种是专业化。** 有的提供方本身就是另一个完整的 agent 运行时——比如把任务交给一个 Codex 进程，或者交给一个 Claude Code 进程。它们有各自的工具集、各自的权限模型、各自的提示词习惯。对 Harness 来说，这只是一个「名字不同的提供方」。

三种动机指向同一个接口，但用法不同：第一种和第三种通常希望「跑完就结束」，第二种希望「尽量同时开始」。这就是下一节要区分的两种形态。

## 2. 委派的两种形态：一次性与可继续

第一种形态叫**一次性子代理（one-shot subagent）**。主代理提交一个提示词，子代理运行一次，返回一个结果，然后结束。它像调用一个函数：给定输入、得到输出、没有后续对话。如果调用方给了 `outputSchema`，结果还会以结构化数据的形式返回，主代理不必从一段自然语言里去抠字段。

第二种形态叫**可继续子代理（continuable subagent）**。它保留一个持久的子会话：主代理可以先发一个提示词，等它跑完，再发第二个提示词，让它接着上一轮的上下文继续做。它还可能被中断——中断当前这一轮，但**不销毁子代理**，之后还能继续对话。这更像「带着一个人一起干活」，而不是「调用一次工具」。

两者的差别不只是「能不能多问一句」。一次性子代理的状态可以随结果一起被丢掉，而可继续子代理必须处理一个更难的问题：**消息什么时候算真正送达。**

设想主代理发了一条消息，进程在下一秒崩溃了。重启之后，这条消息应该被重放，还是应该被当成已经处理过？如果重放，可能重复执行副作用；如果不重放，可能永远丢失。要回答这个问题，系统必须能区分「排队中的消息」和「已经进入子代理上下文的用户消息」。可继续子代理的邮箱（mailbox）就是按这个标准设计的，第 6 节会展开。

## 3. 具名提供方：`ctx.subagents` 为什么是一个注册表

第三讲讲过工具执行器 `ctx.bash`：它是一个**单服务**，整个上下文里只能有一个执行器实现。委派不是这样设计的。

同一个上下文里可以**同时存在多个具名的子代理提供方**，它们注册在 `ctx.subagents` 上，调用方按名字选择。基准版本里有六个兄弟包：

- `dsh-subagent-spawn-in-process`：在当前进程里新建一个子代理；
- `dsh-subagent-fork-in-process`：从父代理当前状态分叉出一个子代理；
- `dsh-subagent-acp`：通过 ACP 协议连接外部 agent；
- `dsh-subagent-codex`：把任务交给 Codex；
- `dsh-subagent-claude-code`：把任务交给 Claude Code；
- `dsh-subagent-dsh-sdk`：在独立的运行时里跑一个 DSH SDK 子代理。

面向模型的消费方是 `dsh-tool-subagent`（按提供方委派）和 `dsh-tool-subagent-control`（可选的全局 `send_message`、`interrupt_agent` 与 `list_agents` 控制工具）。也就是说，模型看到的工具是**一个**，但它背后可以选择**多个**后端。

为什么这里要用注册表，而 bash 用单服务？因为「执行一段 shell」这件事在同一台机器上有唯一正确答案（哪个 shell、哪个沙箱），而「由谁来干这段活」是一个**策略选择**：同一个任务交给 Codex 还是交给 Claude Code，取决于任务本身和用户的配置。注册表让这种选择变成一次按名字查找，而不是一次编译期绑定。

一次启动请求大致长这样（字段名取自源码，`prompt` 在真实类型里是内容块数组）：

```json
{
  "provider": "spawn-in-process",
  "prompt": "找出迁移函数的定义位置并给出文件与行号",
  "parent": "session-root",
  "agentOptions": { "model": "deepseek-v4" },
  "outputSchema": { "type": "object", "required": ["file", "line"] },
  "maxDepth": 1,
  "toolFilter": ["read_file", "search"],
  "persona": "careful-scout"
}
```

其中 `parent` 是**必填**的。它不是一个装饰性的元数据：进程内提供方会从父代理的持久会话状态里推导出工作目录、谱系和委派深度。少了它，子代理既不知道自己该在哪个目录里干活，也不知道自己是第几层。

## 4. 能力预检：fail loud，绝不静默降级

请求里的后五个字段都是**可选**的，而且它们并不对每个提供方都成立。比如 `agentOptions` 里的模型覆盖，对 ACP、Codex 和 Claude Code 来说是没有意义的——这些后端自己决定用什么模型，它们会在启动传输之前**明确拒绝**这个字段。

于是就有了能力标志（capability flags）。每个提供方用一个静态描述符声明自己支持哪些启动期特性：

```ts
interface SubagentCapabilities {
  readonly agentOptions: boolean
  readonly outputSchema: boolean
  readonly depthLimit: boolean
  readonly toolFilter: boolean
  readonly persona: boolean
}
```

服务在**单次 run 存在之前**就用这张表检查请求。如果请求依赖了提供方不具备的能力，结果是抛出 `SubagentError('UNSUPPORTED_CAPABILITY')`，而不是「先接受，然后悄悄忽略这个字段」。

这条规则在文档里有一句很短的总结：**fail loud, no silent degradation**（要么大声失败，要么别降级）。它值得单独记一下，因为「静默忽略」的代价往往在很晚才显现：调用方以为自己拿到了结构化输出，实际拿到的是自由文本；以为自己限定了工具集，实际子代理什么都能用。等到发现时，已经很难追查是哪一层丢掉了请求。

有一个细节能看出这套标志的边界画在哪里：这五个 flag **只描述一次性 `start()` 路径**。可继续子代理不是由提供方组合的，而是由继续执行管理器自己组合的，所以它不由 flag 把关，而由**一个可选方法是否存在**把关——提供方实现了 `prepareContinuable`，就说明它支持可继续子代理；没实现，调用就会发现这个能力不存在。方法即能力，这是类型系统能直接收窄的东西。

最后看 `toolFilter`，它的语义容易误解。文档里的原话是「可见性而非权限」：被过滤掉的工具**既从子代理的提示词里消失，也拒绝执行**。也就是说，子代理不是「看得到但不能用」，而是根本看不见。这是更干净的做法——模型不会浪费一轮去尝试一个注定失败的调用。

## 5. 一次运行的生命周期：深度、容量与结局

一次委派要过三道关。

**第一道是深度。** 委派深度由持久会话头部里的 `delegationDepth`（以及可合并的运行时字段）决定。每向下委派一层，深度加一。默认上限是 `1`：允许直接子代理，不允许「子代理再委派子代理」。这个默认值是有理由的——无限递归委派是很容易写出来的失控模式，而多一层委派带来的收益通常远小于它的成本和不确定性。请求里的 `maxDepth` 可以按次覆盖这个上限，但它本身也要提供方声明 `depthLimit` 能力。

**第二道是容量。** 可继续子代理共享一个名额池，大小由 Host 的 `dsh-subagent` 插件的 `maxActiveSubagents` 设置，**默认 8**。每次新建或冷恢复 Activation 之前都会重新读一遍当前值：调高之后已有树可以接纳更多子代理；调低之后驻留子代理继续运行，但在使用量降到上限以下之前拒绝新的接纳。

容量耗尽的处理方式很值得注意：它**拒绝，而不是排队**，错误码是 `ACTIVATION_LIMIT_REACHED`。文档给的理由很直白：如果排队，就会出现「一个正在等待后代的父代理，又在等待自己占用的名额」这种死锁。同时，一次性运行和外部提供方**不受这个上限约束**——名额池管的是「同一棵可继续父子树里驻留多少个」，不是「一共跑了多少次」。

**第三道是结局。** 一次性子代理以结果结算：成功时带有可选的 `structured` 数据，失败时也有一个**明确的结局**，而不是抛给调用方一个异常。我们的模拟器把它建模成 `SubagentResult`，其中 `stopReason` 只可能是 `completed` 或 `error`：

```python
result = service.start(request)
if not result.ok:
    # stop_reason == "error"，error 里是安全诊断信息
    ...
```

「失败也是返回值」这个选择，和第三讲工具执行器里「工具失败要变成一条错误结果回给模型」是同一个思路：**错误是一种需要被处理的数据，不是一个需要被捕获的意外。** 调用方希望区分「子代理说自己做不到」和「系统坏了」，前者应该是正常的控制流。

## 6. 可继续子级：持久邮箱与 queued-minus-delivered

可继续子代理的模型可以概括成一句话：**一个持久会话，至多关联一个进程内的 Activation（激活进程）。**

「持久会话」说明它的对话历史是写进日志的，重启之后还在。「至多一个 Activation」说明同一时刻只会有一个进程在替它干活。而一个 Activation 可以按顺序跑**多个轮次（turn）**：收到消息、跑一轮、停下、再收到消息、再跑一轮。这就是可继续的含义。

启动时调用方拿到的是 `{ childId, messageId }`。childId 是稳定的子代理身份，后续所有消息都发给它。发消息时，系统按 Activation 的当前状态路由：

- 目标**正在工作**（running）：通过 Steer 把消息送进最近一个 step，而不是另起一轮；
- 目标**空闲**（waiting）：唤醒它，开一轮，并把消息作为这一轮的输入；
- 目标**没有驻留 Activation**：冷恢复（cold resume），重建 Agent 之后再投递。

这里最关键的设计是**邮箱的语义**。文档把它称作 queued-minus-delivered（已排队减去已投递）：Lead Session 先存下完整的排队消息，只有当目标那一侧的待处理 inbox 条目（或已记录的用户消息）完成持久化之后，才写入一条独立的确认事件。恢复时读到的「待处理消息」，就是那些**排了队但还没有落进子代理上下文**的消息。

我们的模拟器用两个列表直接复现了这条规则：

```python
child = service.spawn_continuable(request)
child.send("second question")          # 只入队，不投递
child.pending()                        # -> 排队中、尚未投递的消息
child.run_turn()                       # 投递队首消息，跑一轮
```

`send()` 只入队、`run_turn()` 才投递，这个顺序看起来有点绕，但它正是恢复语义的来源：**只要消息还在 inbox 里，重放就是安全的；一旦它进入了 transcript，重放就必须被阻止。** 如果 `send()` 立即投递，这两个状态就重合了，邮箱也就失去了意义。

## 7. 结算通知：子代理「最后一句话」的归属

还有一个容易被忽略的收尾细节。

当驻留的 Activation 结算（settle）时，管理器会向该子代理**持久化的直接父级**投递一条通知，说明这一段是怎么结束的，并携带子代理**最后一条非空的 assistant 文本块**。如果子代理在结束前什么话都没说，通知里写的是一句固定的占位文本：`It left no closing message.`（它没有留下收尾的话）。

这里有两个设计点。第一，通知的来源类型是 `subagent-settled`，和普通的 `agent-message` **刻意区分开**。因为这条通知是运行时记账产生的，不是子代理自己「说」的话。如果混用同一种来源，父代理的上下文里会出现一条看似由子代理写下、实际由系统代笔的消息——这在调试时会非常误导。

第二，「最后一条非空文本块」这个规则本身就是一次刻意的取舍：子代理可能以一次工具调用结束（没有文本），也可能以一段空白结束。取最后一条**非空文本**，是让父代理至少知道「它最后说了什么」，而不必去解析整段记录。

我们模拟器里的 `settle()` 直接返回这句占位文本，并在关闭的同时**归还名额**：

```python
child.settle()                 # -> "It left no closing message."
service.active_slots()         # 名额已归还
```

注意「归还名额」和「销毁子代理」是两件事：名额只在当前进程里存在，它限制的是**同时驻留多少个**，而不是累计跑过多少个。一个结算了的子代理，它的会话历史仍然在日志里。

## 8. Agent Teams：花名册与共享任务板

`ctx.subagents` 解决的是「我交给谁做」，但它不解决「我们几个人怎么协同」。当主代理不再只派一个子代理，而是拉起一组各司其职的队友时，需要的是一块所有成员都能读写的共享状态。这就是 Agent Teams 这一层，它目前在基准版本里是**实验性**的，需要显式启用。

它的身份体系有三个部分：

- `TeamId` 就是一个 Root `SessionId`——没有另造一个命名空间，一个根会话天然就是一个团队；
- `TeamTaskId` 在团队内按 `task-<n>` 单调分配；
- `TeamMessageId` 是全局随机的，因为消息可能在多个团队之间流转。

团队成员（member）从 `provisioning` 开始，并且**只到达一个终态**：`active` 或 `failed`。这个「只有一个终态」的约束和第四讲里会话事件的追加式模型是同一个思路——状态可以推导，但历史不能含糊。一个成员要么成功进入工作状态，要么带着 `error` 停在失败状态，不存在「半死不活」。

## 9. 任务板：revision 就是 compare-and-set

团队真正共享的是一块**任务板**。每个任务都是一份完整的快照，每次变更都会把 `revision` 加一：

```json
{
  "id": "task-2",
  "revision": 3,
  "subject": "实现迁移脚本",
  "status": "in_progress",
  "ownerId": "session-4",
  "blockedBy": ["task-1"],
  "writeScopes": ["src/storage/"]
}
```

`revision` 的用法是 **compare-and-set**：调用方提交时带上「我以为这个任务是第几版」，如果和当前版本不一致，就拒绝这次修改。我们的模拟器把这一点建模成一个显式的 revision 参数：

```python
board.update("task-1", revision=1, status="completed")   # 成功，revision 变成 2
board.update("task-1", revision=1, status="pending")     # REVISION_CONFLICT
```

这解决的是分布式协作里的经典问题：两个成员都读到「任务 1 尚未完成」，然后各自基于这个认知写入不同的结论。后写入的那次不是「覆盖」，而是**带着过期前提的提交**，应该被拒绝。任务板不做锁定，也不做合并——它只保证「你不会在不知情的情况下覆盖别人的修改」。

任务状态有四个：`pending`（尚未开始，或已经释放）、`in_progress`（**必须携带 owner**）、`completed`、`deleted`（保留的墓碑 tombstone）。`in_progress` 必须带 owner，是因为「有人在做但不知道是谁」这种状态在实践中几乎总是 bug：它无法被中断、无法被追责，也无法判断该不该重新分配。

`blockedBy` 是任务之间的依赖边，它有三条硬约束：

1. 边必须指向**存在**的任务（否则 `UNKNOWN_BLOCKER`）；
2. 边不能指向**已删除**的任务（墓碑不是合法目标，否则 `DELETED_BLOCKER`）；
3. 边不能形成**环**——包括自环（否则 `CYCLIC_BLOCK`）。

我们的模拟器用一个 DFS 做环检测，这在教学上比真实实现简单得多，但它复现了同一条规则：**编辑依赖关系时必须查环，而不是等到执行时才死锁。** 一个 `ready` 的任务，就是它所有的 blocker 都已经 `completed` 的任务。

## 10. writeScopes 只是提示：为什么不做锁

任务快照里还有两个字段经常被误解，值得单独说。

`writeScopes` 是一组**路径前缀**，用来表示这个任务打算改哪些地方。文档明确说明它是**提示性的（advisory），不是锁**。两个正在进行的任务如果 `writeScopes` 有重叠，系统不会拒绝任何一个，只会产生一条警告：

```python
board.write_scope_warnings()
# ['task-1 和 task-2 共享 write scopes；writeScopes 是提示，不是锁']
```

为什么不做成锁？因为「一个路径前缀覆盖了另一个路径前缀」这件事和「两个人真的会改同一个文件」之间还有很大距离。如果按前缀加锁，两个分别修改 `src/a.ts` 和 `src/b.ts` 的任务会因为共享前缀 `src/` 而被拒绝——这是明显的误伤。反过来，如果真的要做文件级锁，那需要的是文件系统层面的事实，而不是模型写进任务快照的一段自我描述。

所以这里的选择是：**把冲突信息暴露给组织者，让有判断能力的那一方决定怎么办。** 警告是有用的信号，锁是错误的约束。这和我们前面反复看到的模式一致——机制只保证它能保证的事，把需要判断的事留给调用方。

## 11. 工作流：模型写的编排脚本

有了委派和共享任务板，还差最后一层：**谁来编排。** 如果编排逻辑只能写成宿主代码，那么每换一种任务模式就得改一次程序。DeepSeek Harness 的答案是让模型写一段**编排脚本**，由 `ctx.workflowEngine` 执行。

启动请求的字段很少：

```ts
interface WorkflowStartRequest {
  script: string                    // 顶层允许 await，以 return <json-value> 结束
  meta: WorkflowMeta                // 身份块，name/description 必填
  args?: unknown                    // 以 args 全局暴露给脚本
  subagentProvider?: string         // 本次运行覆盖引擎级提供方
  maxTotalAgents?: number           // 本次运行的总子代理上限
  parent: Agent                     // 必填：脚本启动的每个子代理都归属于它
  signal?: AbortSignal              // 中止时取消整个运行
}
```

`script` 是一段纯 JavaScript 脚本体，它可以调用引擎提供的全局函数——`agent()` 启动一个子代理，`parallel()` 同时启动几个，`pipeline()` 把上一步的结果传给下一步。脚本以 `return <json-value>` 结束，返回值必须是纯宿主域 JSON 数据。

注意两个「不是」：脚本**不是**用 `eval` 求值的字符串（引擎不会去求值脚本文本来取得 `meta` 和 `args`，它们是普通的 JSON 数据，引擎用 schema 校验 `meta`，在任何工作开始之前就明确拒绝无效数据）；`parent` 也**不是**可选的，理由和第 3 节一样——脚本启动的每一个子代理都需要一个归属，用来继承 cwd、谱系和深度。

一个上下文里只允许一个引擎实现提供 `ctx.workflowEngine`。这和第 3 节的注册表形成了对照：**委派方是多提供方共存，执行引擎是单实现独占**，第二个引擎通过插件配置**替换**第一个，而不是与它并行。

## 12. 上限与封闭的 stopReason

工作流最重要的一条契约是它的终态。

`WorkflowResult.stopReason` 是一个**封闭联合类型**：`completed` | `cancelled` | `error`。只有 `completed` 时 `value` 才有意义；非 `completed` 的原因在 `error` 里携带失败信息，消费方把它映射成一条 `isError` 的工具结果，**而不是把部分输出当成成功上报**。

`agentsStarted` 这个计数字段也有讲究。在正常结算时，它是**脚本侧**的计数——包括那些还在等待并发名额、尚未真正启动的调用。但在终止路径上（取消或进程失败），它**降级为宿主观测到的计数**，因为一个被终止的脚本里还排着多少 `agent()` 调用，此时已经无从得知。文档没有假装这个数字总是精确的，而是直接说明了它在什么情况下退化成什么。

最后是资源管理。`WorkflowRun.result` **绝不拒绝**（never rejects）：脚本失败以 `stopReason: 'error'` 兑现，取消以 `'cancelled'` 兑现。调用方可以等 `result`，可以在执行期间调用 `cancel`，并且**必须在每一条路径上调用 `dispose()`** 来等待脚本与子代理真正静默。也就是说，`result` 的兑现和资源的释放是两件事：拿到结果不代表资源已经清理干净，这是调用方的责任。

## 13. fatal 与 per-item null：错误如何分流

工作流里最容易搞混的是「哪些失败会传播」。这里有一条清楚的规则：**钩子误用是 fatal 的。**

所谓钩子误用，包括：参数写错、传了未知或延迟的选项、超过 `maxTotalAgents` 上限、seam 启动失败、以及整个运行被取消。这些错误以 `WorkflowError` 的形式抛出，并带有一个 `fatal: true` 标记。而 `parallel()` 和 `pipeline()` 这两个组合子**对 fatal 错误直接重抛**，而不是把它映射成 `null`。

只有两类情况会变成「逐项 `null`」：某个**子运行不是 `completed`**（比如子代理自己失败了），以及某个阶段内部的**普通脚本错误**。这两类都不是编排本身的错误，而是「这一步没做成」，所以它们的结果位留空，其余步骤仍然继续。

这条分流的实际意义很大。如果超过上限也被降级成 `null`，那么脚本会「安静地」拿到一堆空值，继续往下跑，最后返回一个看起来正常、实际缺了一半的结果。而「上限被突破」是调用方必须立刻知道的事——它说明脚本的设计和上层配置不匹配。

在我们的模拟器里，两种情况的差别在结算原因上直接可见：

```text
ceiling -> error | run accepted more than 2 agents
child failure -> completed | value: ['done: ok', None]
```

第一行是 fatal 的钩子误用：整个运行以 `error` 结束。第二行是子代理失败：运行本身照常 `completed`，失败的那一项是 `None`。**「系统出错」和「这一步没做成」是两种不同的东西**，把它们分开，调用方才有可能做对决策。

## 14. 事件只是观察：快照、克隆与省略的 value

工作流运行期间会发出一系列 `workflow/*` 事件，供观察者（UI、日志、遥测）使用。这些事件的设计同样是防御性的。

每个事件的 payload 都以一个 `WorkflowRunInfo` 开头，它只包含 `id` 和 `meta` 这两个字段。事件是**仅供观察的**：监听器拿不到 `cancel`，也拿不到 `dispose`。换句话说，观察者能看，但不能控制运行——控制权只在持有这个 run 句柄的调用方手里。

还有两个细节。第一，`workflow/end` 这个事件**刻意省略了 `result` 的 value**。原因是 value 可能很大，而事件会被广播给多个订阅者；把最终结果塞进一条广播事件，既浪费又容易造成泄漏。需要结果的人去等 `result`，观察者只需要知道结局是什么。第二，每个监听器收到的是**自己的一份 payload 克隆**（浅拷贝）。一个监听器如果就地修改了 payload，不会影响另一个监听器看到的内容。

我们的模拟器把这三条都实现了出来，运行结果如下：

```text
payload keys: ['kind', 'run', 'stop_reason']
has cancel? False
```

前两行合起来说明：监听器看到的是一个包含 `kind`、`run` 快照和结局的结构，而 `run` 快照上没有任何控制方法。

## 15. 对照源码

本轮使用的提交是 `5badb15009ae1756c3afe0ae0cef1faafc290ccc`（根包版本 `0.2.1-alpha.1`）。

- [subagent 子系统](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/subsystems/subagent.zh.md)
- [agent-team 子系统](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/subsystems/agent-team.zh.md)
- [workflow 子系统](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/subsystems/workflow.zh.md)
- [subagent 包说明](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/subagent/subagent/README.zh.md)
- [agent-team 包说明](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/experimental/agent-team/README.zh.md)
- [workflow 包说明](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/workflow/workflow/README.zh.md)

重点看：

- `subagent.zh.md` 里 `SubagentCapabilities` 与 `SubagentStartRequest` 的类型定义旁，为什么每个 flag 都对应一个具体请求字段；
- `subagent.zh.md` 中「可继续后台 subagent」一节如何用持久 Session 加至多一个 Activation 定义生命周期；
- `agent-team.zh.md` 里 task event 的完整快照、`revision` 的 compare-and-set 用法，以及 `writeScopes` 的提示性说明；
- `workflow.zh.md` 里 `WorkflowStartRequest`、`WorkflowMeta`、`WorkflowResult` 与 `WorkflowRun` 四段类型定义的注释。

## 16. 思考与练习

1. 一个任务需要读三十个文件才能得出结论，但这个结论只有两行。用一次性子代理和可继续子代理分别怎么做？哪一种更合适，为什么？
2. 如果 `toolFilter` 的语义改成「工具可见但调用会被拒绝」，对模型的行为和 token 消耗各有什么影响？为什么「从提示词里消失」是更好的做法？
3. 尝试给任务板加一个状态：`blocked`（依赖未满足）。它应该是一个新的状态，还是继续用 `pending` 加 `blockedBy` 表达？说出你的理由。
4. 假设把「超过 `maxTotalAgents`」也改成映射为逐项 `null`，会发生什么？举一个具体的脚本例子说明后果。
5. 为什么邮箱的语义必须是 queued-minus-delivered，而不是「send 即投递」？如果反过来，崩溃恢复时会出现什么问题？

<details>
<summary>自查要点</summary>

1. 一次性子代理隔离过程、只回传结论；可继续子代理在你的主代理需要「追问」或「分阶段推进」时更合适。如果只跑一次，用一次性，别为一次调用维护一个持久会话。
2. 可见但拒绝会浪费一轮模型调用去尝试注定失败的工具，还可能在提示词里暗示这个能力存在。从提示词里消失更干净：模型不会尝试它看不见的东西。
3. 两种都能表达依赖，但「状态」和「原因」是两件事。把 `blocked` 做成独立状态，就必须在依赖完成时由别处把它改回来，多了一份需要同步的状态；用 `pending` + `blockedBy` 表达，依赖关系只有一份真相，`ready` 是一个纯函数式的推导。
4. 运行会「安静成功」，但结果里缺了一半的项，调用方看到的是一份看起来正常的部分输出。这比直接失败更危险，因为失败至少是可观测的。
5. send 即投递时，「已排队」和「已进入上下文」重合，恢复时无法区分该重放哪些消息：重放会重复执行，不重放会丢失。区分这两个状态，才让重放变得安全。

</details>

## 17. 当前实现范围

本讲的离线实验 `examples/agent_team.py` 演示：

- 具名提供方注册表与启动前能力预检（缺能力时明确拒绝，不静默忽略）；
- 默认深度 1、按次 `maxDepth` 覆盖，以及超深时的 `DEPTH_LIMIT_EXCEEDED`；
- 一次性子代理失败是 stop reason 而不是异常；
- 可继续子代理的 queued-minus-delivered 邮箱、FIFO 轮次、中断不销毁、结算归还名额与占位收尾文本；
- 容量用尽时拒绝而不是排队，以及失败的启动不泄漏名额；
- 任务板的 revision compare-and-set、`in_progress` 必须有 owner、`blockedBy` 的存在性／墓碑／环校验，以及只产生警告的 `writeScopes`；
- 工作流的 `agent()`／`parallel()`／`pipeline()`、`maxTotalAgents` 上限、封闭的 `stopReason`，以及只传快照的观察事件。

它没有实现真实的并发或线程、没有连接真实 ACP／Codex／Claude Code 传输、没有冷恢复与会话持久化、没有 `fork` 上下文继承、没有 token 预算、没有真实的取消句柄清理、没有邮箱去重，也没有 Web 投影。

2026-10-16 已在 Windows、Python 3.13.2 下运行本讲示例，并通过 52 项行为测试。文档中的 JSON 示例和本地章节链接也经过检查。本轮未运行在线模型，也未启动 DeepSeek Harness。

下一讲讨论独立开发与评估：怎样为一条模型驱动的路径写出可重复的验证，什么时候该 mock、什么时候必须用真实组件，以及为什么「测试通过」和「行为正确」是两件不同的事。

[返回学习路线](README.md) · [上一讲](08-harness-assembly.md)