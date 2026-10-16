# 第五讲：上下文管理怎样决定模型看见什么

[返回学习路线](README.md) · [上一讲：会话与状态](04-session-and-state.md)

第四讲把 Agent 的状态从易失的聊天数组搬进了只追加的事件日志。日志解决了“历史从哪里来、崩溃后怎样恢复”的问题，但还没有回答另一个更直接的问题：

> 每个模型请求，究竟应该从这份历史里拿多少内容？

真实任务会同时积累用户目标、工具结果、文件片段、错误信息、工作区规则、技能说明和长对话。模型的一次调用却有固定上下文窗口。把所有内容原样塞进去，轻则成本上升、关键指令被淹没，重则请求直接超限。

这一讲把上下文当作“每个请求都要重新装配的结果”，依次讨论预算与优先级、工作区指令、技能按需加载和历史压缩。配套实验 `examples/context_budget.py` 仍然离线运行，只使用标准库。

## 1. 上下文不是聊天记录，而是一次装配结果

假设用户要求修改 `packages/api/handler.ts`。一次真实请求可能同时携带：

- 当前系统提示词；
- 可用工具的 JSON Schema；
- 从会话日志投影出的消息历史；
- 用户全局和项目工作区指令；
- 运行时环境快照；
- 技能目录；
- 之前的压缩摘要；
- 当前用户请求；
- 输出长度上限等采样参数。

这些内容的生命周期不同：

| 内容 | 主要来源 | 是否每次重建 |
|---|---|---|
| 系统提示词 | 系统提示词插件按顺序组装 | 每个步骤都可能重建 |
| 工具 schema | 当前可用工具注册表 | 工具集合变化时变化 |
| 消息历史 | 会话日志的 surface 投影 | 每个步骤读取当前投影 |
| 工作区指令 | 全局、项目和目录层级文件 | 发现、更新或移除时追加事件 |
| 技能目录 | 当前可见技能的名称和描述 | 目录变化时整批替换 |
| 技能正文 | 模型调用 `skill` 工具或用户显式调用 | 按需加载 |
| 压缩摘要 | 旧历史被压缩后生成的替换消息 | 成为新的历史前缀 |

如果只写：

```python
prompt = system_prompt + "\n" + "\n".join(message.content for message in history)
```

程序仍然可能得到一句“看起来正确”的文本，但它已经丢失了装配过程需要的信息：

- 这段文字属于 system、developer、user、tool 还是某类派生上下文；
- 每个片段来自哪个文件、技能或压缩事件；
- 哪些片段必须保留，哪些可以丢弃；
- 预算不足时先删谁、后删谁；
- 内容变化后，模型请求的哪一段前缀还能复用；
- 崩溃恢复时，这些片段能否从日志重新构造。

所以更合适的做法不是维护一个不断变大的字符串，而是先构造“上下文块”，再做选择和渲染。

配套实验定义了最小模型：

```python
@dataclass(frozen=True)
class ContextBlock:
    key: str
    role: str
    content: str
    kind: str
    required: bool = False
    priority: int = 0
    order: int = 0
```

- `key` 用来检测重复块，也让日志和测试可以稳定引用它；
- `role` 决定它进入模型消息时使用什么角色；
- `kind` 说明来源，例如 `system`、`request`、`history`、`reference`；
- `required=True` 表示不能为了节省预算静默删除；
- `priority` 只决定选择顺序；
- `order` 决定最终渲染顺序。

这里故意把“选择顺序”和“展示顺序”分开。最该保留的内容不一定应该出现在最前面；最终消息顺序仍然要符合模型协议和指令优先级。

## 2. 预算与优先级

上下文窗口不是一个模糊的“大约多少字”，而是模型、消息格式、工具 schema 和输出预留共同决定的硬上限。即使只统计消息正文，也要考虑每条消息的角色包装、工具定义和推理模型的内部 token。

教学实验使用 UTF-8 字节数，因为它确定、直观、无需模型分词器：

```python
def byte_length(value):
    return len(value.encode("utf-8"))
```

`assemble_context(blocks, max_bytes)` 的规则是：

1. 拒绝重复 key；
2. 拒绝无效预算；
3. 先放入所有必需块；
4. 如果必需块已经超预算，抛出 `REQUIRED_CONTEXT_TOO_LARGE`；
5. 可选块按 `priority` 从高到低尝试加入；
6. 最终按 `order` 稳定排序并渲染。

可以用一个例子观察这套规则：

```python
selection = assemble_context(
    [
        system_rule,       # required, order=0
        huge_reference,    # optional, priority=100
        old_history,       # optional, priority=10
        current_request,   # required, order=50
    ],
    max_bytes=40,
)
```

当 `huge_reference` 装不下时，它会先被放弃，哪怕它的 priority 最高。priority 表达的是“在可选内容中先考虑谁”，不是“可以挤掉系统规则和当前请求”。

必需块超预算时为什么不截断？因为不同内容的失败方式不同：

- 截断当前用户请求，模型可能执行错误任务；
- 截断系统安全规则，程序可能越过权限边界；
- 截断工具调用结果，模型可能把未知状态当成成功；
- 静默删除必需块，问题直到行为出错才暴露。

更好的行为是明确失败，或者由上层先做专门的有损处理。例如工具结果可以经过显式的 pruner 替换，但普通上下文装配器不应该在没有记录的情况下偷偷截断。

在真实实现中，预算计算至少还要加入：

- 每条消息的协议开销；
- 工具名称、描述和参数 schema；
- 系统提示词的动态片段；
- 当前请求要求保留的输出空间；
- 不同 provider 对图像、文件和工具消息的计费方式。

所以“字节预算”只适合教学演示。生产实现应当使用目标模型和路由的真实 token meter。

## 3. 工作区指令：持久、有序、有来源

项目根目录通常有 `AGENTS.md`，用户主目录也可能有全局规则。一个包内还可能有更具体的 `AGENTS.md`。这些内容的优先级是：

```text
用户全局指令
    ↓ 更具体
项目根指令
    ↓ 更具体
当前工作目录链上的指令
```

DeepSeek Harness 的 `agent-instructions` 会把它们渲染成一条持久基线 user 消息，顺序从宽泛到具体。帧大致如下：

```text
<system-reminder>
The following workspace instructions may be relevant to your work.
Use them as guidance when applicable.
More specific instructions take precedence over broader ones.
They do not override system, developer, or direct user instructions.

Instructions from: ~/.dsh/AGENTS.md

<user-global-instructions>

Instructions from: AGENTS.md

<project-instructions>
</system-reminder>
```

这里有几个容易忽略的设计点。

### 3.1 指令是 user 消息，不是看不见的 system 后缀

工作区指令在模型协议里作为带来源的 `user/message` 事件存在。它不会偷偷拼进系统提示词，也不会绕开会话日志。

这样做的好处是：

- 模型看到的内容可以从日志重建；
- 来源路径、发现顺序和更新事件都能审计；
- 压缩器知道它属于哪类上下文；
- 用户可以把“项目文件中的建议”和真正的系统规则区分开；
- 恢复后不会因为某个内存缓存丢失而得到另一套指令。

`<system-reminder>` 只是模型可读的帧标记，不代表它的权限等同于系统指令。帧内文本甚至要转义内容里的 `</system-reminder>`，防止文件正文提前闭合外层结构。

### 3.2 预算不足时先丢宽泛文件

指令集合可能在 UTF-8 字节预算内选择：

1. 如果完整集合能装下，全部保留；
2. 如果装不下，先丢弃完整的较宽泛文件；
3. 如果只剩最具体文件仍装不下，就在 UTF-8 码点边界内截断它；
4. 渲染可见的省略和截断通知。

这个顺序背后的判断是：当前目录的规则通常比全局偏好更直接影响这次修改。全局里没装下的内容被显式列出，模型和用户都能知道发生了损失。

配套实验的核心代码是：

```python
ordered = sorted(files, key=lambda file: (file.depth, file.path))
full = _render_instruction_text(ordered)
if byte_length(full) <= max_bytes:
    return InstructionRender(full, tuple(ordered), (), ())

for start in range(1, len(ordered)):
    included = ordered[start:]
    omitted = ordered[:start]
    candidate = _render_instruction_text(included, omitted)
    if byte_length(candidate) <= max_bytes:
        return InstructionRender(candidate, tuple(included), tuple(omitted), ())
```

如果连最具体文件也超出剩余预算，就对它的内容做二分搜索，找到最大的可容纳字节数。截断后仍要保证：

- 不切断 UTF-8 码点；
- 外层 `</system-reminder>` 仍然存在；
- 渲染帧本身也计入预算；
- 截断了多少字节有结构化记录，而不是只在内存里丢一个布尔值。

Harness 中 `dsh-base` 的默认 `maxBytes` 是 65,536；`agent-instructions` 包本身要求调用方显式传入 `maxBytes`。这说明预算属于装配策略，不应该散落在渲染函数里写死。

### 3.3 新目录、更新与移除都追加事件

指令不是只在会话开始时发现一次。成功的第一方 `read`、`write` 或 `edit` 到达更深目录后，下一次请求可以追加新适用的指令。

已有文件内容变化时，会追加一条更新说明；文件消失或变成重复内容时，会追加移除说明。例如概念上是：

```text
Instructions updated from: packages/api/AGENTS.md
Instructions removed: packages/api/AGENTS.md
```

没有文件系统 watcher，也不会因为 shell 执行了 `cd` 就自动发现新语法规则。只有受控的目录访问事件会触发嵌套发现。

为什么更新要追加新消息，而不是改写旧消息？

- 会话日志是 append-only；
- 旧请求的前缀需要保持可审计；
- KV Cache 依赖稳定前缀；
- 模型需要知道“规则发生了变化”，而不只是看到一个被替换后的结果；
- 崩溃恢复不应依赖某次内存中的文件监听回调。

指令正文虽然不进入摘要，但整个指令基线可以随事件替换。这个区别很重要：压缩器不会把文件规则概括成一句模糊的摘要，但基线更新机制可以让新规则替换旧规则。

## 4. 技能：目录先给摘要，正文按需加载

如果项目里有几百个技能，把每个 `SKILL.md` 全文都放进系统提示词会很快耗尽上下文。Skills 机制把生命周期拆成两层：

- 目录：让模型知道“有哪些能力可用”；
- 正文：只在真正调用时加载。

DeepSeek Harness 的文件技能可以在 `<root>/<name>/SKILL.md` 或 `<root>/<name>.md` 找到，且刻意只发现一层。YAML frontmatter 至少要有：

```yaml
name: release-check
description: Verify release metadata and changelog before a tag.
```

还可以提供 `whenToUse`、`metadata`、`disable-model-invocation` 和 `user-invocable`。发现阶段只解析摘要，不把正文常驻在目录里。

### 4.1 目录只包含名称和描述

`tool-skill` 在首次请求前发布持久目录 user 消息，默认把每条描述限制在 500 个字符。目录条目大致是：

```text
- release-check: Verify release metadata and changelog before a tag.
```

配套实验用 `SkillRegistry` 展示这一区分：

```python
skills.register(Skill(
    "release-check",
    "Verify release metadata and changelog before a tag.",
    "Full release instructions: ...",
))

catalog = skills.render_catalog()
assert "release-check" in catalog
assert "Full release instructions" not in catalog

loaded = skills.load("release-check")
assert "Full release instructions" in loaded
```

模型真正调用技能时，得到的是带帧的正文：

```markdown
<skill_content name="release-check">
<skill_resources>
...资源指引...
</skill_resources>

<skill_instructions>
...完整技能指令...
</skill_instructions>
</skill_content>
```

这个结果作为普通工具历史保留。后续请求会继续看到它，直到历史被压缩或替换。技能正文没有独立的版本化机制，所以“同名技能后来改了文件”不会自动让已经加载过的旧正文消失。

### 4.2 目录 digest 不应该依赖正文

如果每次编辑技能正文都让目录 digest 变化，系统就会发布一份内容完全相同的新目录，无谓地破坏请求前缀和 KV Cache。

正确的摘要边界是“目录条目”，即名称和截断后的描述：

```python
before = registry.catalog_digest()
registry.replace_body("release-check", "new body")
assert registry.catalog_digest() == before
```

目录真的增加、删除或改名时，才发布完整替换目录。空目录也要发布一个明确结果，否则旧名称会继续被模型当成可用能力。

### 4.3 模型加载与用户显式调用

技能可以标记是否允许某类调用：

| 配置 | 模型目录 | 模型 `skill` 工具 | 用户 `/name` |
|---|---:|---:|---:|
| 默认 | 可见 | 可用 | 可用 |
| `disable-model-invocation: true` | 不可见 | 不可用 | 可用 |
| `user-invocable: false` | 可见 | 可用 | 不可用 |

非模型可调用技能不会出现在目录里，模型即使猜到名字直接调用 `load()`，实现也应当拒绝，而不能把“目录不可见”当成隐晦的权限边界。

用户显式调用 `/name` 可以直接注入用户可调用技能的正文。模型通过 `skill` 工具加载和用户显式调用应当共享同一个规范渲染器，否则正文的帧、资源和转义规则会出现两套实现。

技能机制也有已知限制：

- 已经加载的正文没有大小上限；
- 目录是全量替换，不是增量更新；
- 正文修改不会自动改写已经出现的工具结果；
- 资源字段通常只是路径或使用指引，不会自动读取；
- 加载主要是把文本放进历史，不是执行沙箱代码。

## 5. 压缩：替换的是模型可见投影，不是原始日志

即使用了工作区指令预算和按需技能，长任务仍会积累大量工具结果和中间推理输出。压缩的目标不是删除日志，而是把“旧的前缀”替换成一个更短的摘要。

假设 surface 历史是：

```text
0 user        修复第一个问题
1 assistant   调用 read_file(call_1)
2 tool        call_1 的结果
3 assistant   调用 read_file(call_2)
4 tool        call_2 的结果
5 user        现在修复第二个问题
```

压缩可以选择前三条节点，生成一条摘要，模型看到的投影变成：

```text
0 user        <compacted-summary>...</compacted-summary>
3 assistant   调用 read_file(call_2)
4 tool        call_2 的结果
5 user        现在修复第二个问题
```

原始日志里的 0、1、2 仍然存在，`compaction/summary` 还记录了它们被哪些事件遮蔽。只是 surface 投影不再让模型看到原始正文。

### 5.1 压缩边界不能拆开工具调用对

最危险的压缩错误是只保留 assistant 的 tool call，却把对应 tool result 删掉，或者反过来。模型会收到不完整的工具协议，恢复逻辑也会失去因果配对。

所以压缩器要选择平衡的区间：

- 从首个非 system surface 节点开始；
- 保留近期尾部；
- 边界可以向左移动；
- assistant tool call 和它的全部结果必须待在同一个可见区域；
- 压缩后的替换帧本身也要计入预算。

配套实验的 `_balanced_prefix_end` 会不断向左移动候选边界，直到被选前缀和保留尾部各自都没有悬空的 call 或 result。

### 5.2 压缩事务只产生一个 surface 变化

DeepSeek Harness 的压缩事务包含以下事件：

```json
{
  "events": [
    {
      "type": "compaction/start",
      "data": {
        "turn": 0
      }
    },
    {
      "type": "compaction/summary",
      "data": {
        "summary": "Earlier work: two file reads completed.",
        "shadowed_range": {
          "start": 0,
          "end": 2
        },
        "shadowed_seqs": [0, 1, 2]
      }
    },
    {
      "type": "user/message",
      "data": {
        "text": "<compacted-summary>...</compacted-summary>",
        "surface_op": {
          "op": "replace",
          "start_seq": 0,
          "end_seq": 2
        }
      }
    },
    {
      "type": "compaction/end",
      "data": {
        "turn": 0
      }
    }
  ]
}
```

其中：

- `compaction/start`、`compaction/summary`、`compaction/end` 只写入日志，不直接进入模型 surface；
- 只有带 `surface_op: replace` 的 `user/message` 会改变模型可见投影；
- `shadowedSeqs` 才是权威的被遮蔽事件集合；
- `start` 和 `end` 表示当前 surface 的位置跨度，不是可以随意比较大小的原始日志 seq；
- 之前的 replace 可能让表面位置关系变得复杂，因此实现不能只靠数值区间猜测。

压缩成功后，后续请求以替换消息为新的历史前缀。旧日志可回放、可审计，压缩记录也知道自己遮蔽了什么。

### 5.3 崩溃和失败时的状态

压缩不是一次原子内存操作，而是一段可能跨模型请求的持久化事务：

```text
compaction/start
    → 调用摘要模型
    → 校验摘要结果
    → compaction/summary + replacement user/message
    → compaction/end
```

如果进程在开始之后、结束之前崩溃，日志里会留下未匹配的 `compaction/start`。恢复时不能假装压缩已经完成，也不能盲目重新压缩，因为可能已经落下一部分替换事件。

实现需要分别检查：

- 是否存在未结束的压缩事务；
- 当前 turn 是否仍未结束；
- 摘要结果是否为空或包含不支持的内容；
- 摘要估算后是否真的比被遮蔽区域更小；
- 摘要请求失败后，最新持久 surface 是否仍然完好。

摘要失败时保留最新持久 surface；已经成功执行的剪枝或替换不会被回滚。压缩工具结果修剪器也是类似思路：它保留完整事件数据，只替换面向模型的 `content`，让模型少看冗长正文，但日志仍然能审计原始结果。

### 5.4 阈值与保留比例

`compaction-basic` 的默认策略可以用公式描述。设：

- `W` 为上下文窗口；
- `O` 为输出预留；
- `B = 65536` 为保底余量。

默认触发阈值大致是：

```text
floor(min(W * 0.8, W - O - B))
```

近期需要保留的目标约为 `W - O` 的 16%，默认 `retainRatio` 是 0.16。这个设计避免等到请求已经溢出才压缩，也为摘要请求和工具输出留出空间。

配置还可以调整：

- 触发阈值比例；
- 预留 token；
- 近期保留比例或绝对 token 数；
- 摘要使用的 provider 和 model；
- 摘要输出上限；
- 重试次数和溢出重试次数；
- 特定模型的策略。

当模型返回 `CONTEXT_WINDOW_EXCEEDED` 时，循环可以先压缩再重试。但重试必须有上限，不能把上下文错误变成无限递归。

### 5.5 摘要模型看到什么

摘要请求不是把一整份新系统提示词发给另一个无状态模型。它保留原请求的系统提示词、工具 schema 和消息前缀，再把被遮蔽历史和一条固定的压缩指令接在后面。

因此，摘要调用可以复用上一轮路由请求的 KV Cache。摘要模型看不到私有推理过程，只看到允许它读取的历史和最后一条压缩指令。返回结果只保留文本，并且必须：

- 非空；
- 不包含图像等不支持的内容；
- 比被遮蔽内容的估算更小；
- 能被可靠地包进 `<compacted-summary>` 帧。

若摘要请求失败，主会话不会被回滚到更早状态。下一次请求仍然使用已落地的持久 surface。

## 6. 三者在请求序列中怎样汇合

把前面的机制放在一个步骤里，顺序大致是：

```text
会话日志
  ├─ 工作区指令事件
  ├─ 技能目录事件
  ├─ 压缩 replace 事件
  └─ 普通 user / assistant / tool 事件
        ↓
surface 投影
        ↓
+ 每步组装系统提示词
+ 当前工具 schema
+ 动态运行时上下文
+ 当前用户请求
        ↓
预算检查、压缩或溢出恢复
        ↓
provider 请求
```

不同内容的持久化方式并不相同：

- 系统提示词和工具 schema 是装配结果，可能在每一步变化，但它们会以 `system/message` 等形式进入会话历史；
- 工作区指令和技能目录是持久 user 消息，属于派生上下文；
- 技能正文是普通工具结果；
- 压缩摘要是带 replace 操作的持久 user 消息；
- 工具调用和结果仍然属于原始事件序列。

模型请求头部不一定单独携带一个 `system` 字段；在很多实现里，系统提示词也会先成为 surface 中的 `system/message` 节点，再由 provider 适配层转换。这个细节必须和模型协议分开看。

无论采用哪种表示，都要保持一个原则：

> 凡是模型看见的内容，都必须能从会话日志重建。

否则测试无法解释行为，恢复无法复现输入，压缩也无法准确判断哪些内容仍在 surface 中。

上下文变化还会影响 KV Cache。系统提示词、工具定义、目录和消息顺序不变时，请求前缀可以复用；一旦某个早期片段变化，从首个变化 token 之后的内容都可能需要重新计算。压缩尤其如此：`surface_op: replace` 会开启一段新的可见历史，后续请求不能再假设旧前缀原样存在。

## 7. 对照 DeepSeek Harness 源码

本轮仍固定使用提交 `5badb15009ae1756c3afe0ae0cef1faafc290ccc`。可以按下面顺序阅读。

### 系统提示词和工具 schema

- [system-prompt 包说明](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/core/system-prompt/README.zh.md)

重点看：

- 段按 `order` 升序拼接；
- `complete: true` 段组成完整提示词；
- 多个有效 complete 段会失败；
- 未解析变量或无效组合不会发出格式错误请求；
- 动态运行时上下文是带来源的 user 快照；
- 系统提示词和工具 schema 都会影响 KV Cache 前缀。

### 工作区指令

- [agent-instructions 包说明](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/context/agent-instructions/README.zh.md)
- [指令渲染实现](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/context/agent-instructions/src/render.ts)

重点看：

- 从宽泛到具体的加载顺序；
- 同内容去重；
- 先丢宽泛文件、再截断最具体文件；
- UTF-8 安全截断；
- `</system-reminder>` 转义；
- 默认 65,536 字节预算；
- 更新和移除为何追加事件。

### 技能发现、注册和加载

- [skill 注册表](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/skill/skill/README.zh.md)
- [文件系统发现](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/skill/skill-filesystem/README.zh.md)
- [tool-skill 目录与加载器](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/skill/tool-skill/README.zh.md)

重点看：

- 技能根目录的 rank 和项目根判定；
- frontmatter 必填字段；
- 目录和正文分离；
- 默认 500 字符描述截断；
- 目录 digest 只依赖条目；
- 非模型可调用技能如何从目录和 loader 中排除。

### 压缩与修剪

- [compaction 服务](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/compaction/README.zh.md)
- [compaction-basic 策略](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/compaction/compaction-basic/README.zh.md)
- [压缩区间选择](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/compaction/compaction-basic/src/region.ts)
- [摘要请求与帧](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/compaction/compaction-basic/src/summarizer.ts)
- [压缩事件类型](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/compaction/compaction/src/types.ts)
- [压缩子系统文档](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/subsystems/compaction.zh.md)

重点看：

- `compaction/start` 的锁和未匹配 start；
- 只有 replacement user message 改变 surface；
- `shadowedSeqs` 与 surface position span 的区别；
- 工具调用对平衡；
- 默认阈值和 16% 保留比例；
- 摘要失败不回滚已落地的剪枝；
- 工具结果修剪器的 8192 / 4096 / 1024 默认值。

## 8. 思考与练习

1. 为什么上下文不应该等价于“把历史数组拼成一个字符串”？
2. `priority` 和 `order` 分别解决什么问题？为什么不能只保留一个字段？
3. 必需块超预算时，为什么直接截断可能比报错更危险？
4. 工作区指令为什么作为带来源的 user 消息，而不是隐藏的 system 文本？
5. 技能目录为什么不包含技能正文？正文编辑为什么不应改变目录 digest？
6. 压缩为什么必须选择平衡的工具调用边界？拆开会造成什么后果？
7. 压缩事务中，哪些事件只写日志，哪个事件真正改变模型可见的 surface？
8. 如果摘要请求失败但剪枝已经提交，为什么通常保留最新状态而不是整体回滚？

<details>
<summary>自查要点</summary>

1. 字符串会丢失角色、来源、优先级、预算、选择记录和恢复边界；后续无法解释模型为什么看见这段内容，也无法稳定重建请求。
2. priority 决定预算不足时先保留哪些可选块；order 决定最终消息的排列顺序。选择策略和协议顺序是两个问题，混在一起会导致“最该保留”被误当成“必须最先出现”。
3. 静默截断会改变语义。当前请求、安全规则或工具结果被截断后，模型可能执行错误任务或把未知状态当成功；明确失败能把损失交给上层处理并留下记录。
4. 这样才能从日志重建来源、顺序和更新过程，区分类似文本的不同权限来源，并让压缩、测试和恢复看到同一份输入。
5. 目录需要长期可见，正文只在实际调用时消耗 token。正文变化不影响目录条目，因而不会发布无效新目录、打断前缀和缓存。
6. 压缩边界不能留下孤立的 call 或 result，否则模型收到非法工具协议，恢复程序也无法判断调用是否完成。边界向左移动可以保住最近配对。
7. `compaction/start`、`compaction/summary`、`compaction/end` 只记录事务和摘要数据；带 `surface_op: replace` 的 `user/message` 才真正使旧区间在 surface 中不可见。
8. 最新状态可能已经包含成功提交的剪枝或替换，整体回滚会丢失已经落地的进度，还可能让下一次恢复基于错误假设。系统应保留现状并明确报告未完成事务。

</details>

## 9. 当前实现范围

本讲的离线实验 `examples/context_budget.py` 演示：

- 指令文件从宽泛到具体的预算选择、省略和 UTF-8 边界截断；
- 必需块与可选块不同的预算策略；
- priority 决定选择、order 决定渲染顺序；
- 技能目录只发布名称和描述，正文按需加载；
- 编辑正文不改变目录 digest；
- 压缩把旧前缀替换成摘要投影，但保留原始历史；
- 压缩边界不会拆开工具调用配对；
- `compaction/*` 事务事件与唯一的 surface replacement 分离。

它没有实现真实 tokenizer、provider token 计费、磁盘持久化、并发压缩锁、KV Cache 管理、真实文件监听、技能资源读取、模型摘要调用或在线模型请求。字节预算只是一个离线的可观察模型，不能替代目标模型的实际 token 统计。

2026-10-16 已在 Windows、Python 3.13.2 下运行本讲示例，并通过 17 项行为测试。文档中的 JSON 示例和本地章节链接也经过检查。本轮未运行在线模型，也未启动 DeepSeek Harness。

下一讲会进入可靠的循环：流式输出怎样增量落地，重试和取消如何协同，取消怎样传播，以及并发和未完成工具调用怎样收尾。

[返回学习路线](README.md) · [上一讲](04-session-and-state.md)