# 第七讲：Cordis 插件基础——服务、依赖、事件与生命周期

[返回学习路线](README.md) · [上一讲：可靠的循环](06-reliable-loop.md)

前六讲一直在往外看：模型调用、工具执行、会话日志、上下文装配、流式与重试。从这一讲起，视线转向内侧——DeepSeek Harness 自己是怎样把成百上千个能力拼起来的。

答案是一个叫 **Cordis** 的插件框架。Harness 里的工具注册表、模型服务、Agent 驱动器、会话存储、压缩、技能、子 Agent……全部都是 Cordis 插件。理解 Cordis，才能真正读懂「`ctx.tools` 为什么存在」「为什么插件卸载时监听器会自动消失」「为什么一个服务没了，依赖它的插件会先停下来」。

配套实验 `examples/cordis_kernel.py` 用纯 Python 实现了一个几十行的迷你内核，复现了服务容器、依赖等待、五种事件分发、可逆副作用、子插件和配置热替换。它同步、单线程，规模远小于真实框架，但关键语义是对齐的。

## 1. 从「直接 import」回到「通过 ctx 找服务」

先看一个反面写法。假设要写一个「把每次工具结果记下来」的功能：

```python
from harness.tools import REGISTRY
from harness.logging import LOGGER

def install():
    REGISTRY.hooks.append(logger_hook)
    LOGGER.setup()
```

这段代码能在小项目里跑，但在 Harness 这样的系统里有三个问题。

**第一，它把实现钉死了。** `REGISTRY` 是哪一个工具注册表？测试时想换成假实现怎么办？部署时想接不同的日志后端怎么办？

**第二，它没有生命周期。** 这个功能什么时候该生效、什么时候该撤销？如果有人卸载这个功能，`REGISTRY.hooks` 里那条 hook 谁来删？`LOGGER` 谁来关？

**第三，它没有依赖声明。** 代码自己假设 `REGISTRY` 已经初始化好了。可在真实系统里，加载顺序是不确定的：日志插件的 `apply` 可能在工具服务就绪之前就被调用。

Cordis 的答案是把这三件事都收进一个 `ctx` 对象：**能力通过服务暴露、依赖通过声明表达、副作用通过注册撤销。** 插件不 import 别的插件，它声明「我需要 `tools`」，然后直接用 `ctx.tools`。框架负责在合适的时机加载它。

## 2. 插件有三种形态，语义相同

在 Harness 里写一个最简插件，只需要一个文件加一个函数：

```ts
export const name = 'hello-plugin'

export function apply(ctx: Context) {
  // 依赖已经就绪，在这里注册能力。
}
```

这个函数就是插件本身。对象形式只是把它包起来：

```ts
export default {
  name: 'my-plugin',
  inject: ['tools'],
  apply(ctx: Context) {
    // ...
  },
}
```

第三种是类形式，用于**向外提供服务**：

```ts
export default class MetricsService extends Service {
  static inject = ['llm']

  constructor(ctx: Context) {
    super(ctx, 'metrics')   // 服务的稳定名字是 'metrics'
  }

  record(event: string, value: number) { /* ... */ }
}
```

`super(ctx, 'metrics')` 这一行把实例注册成一个服务，之后任何插件都可以通过 `ctx.metrics` 使用它。类形式适合「这个插件本身就是一项能力」的场景；如果只是监听事件或注册工具，函数形式就够了。

教学实验里三种形态对应三种说法：

```python
@plugin(name="reporter", inject=("metrics",))
def reporter(ctx, config):
    ...
```

`plugin(...)` 装饰器把一个普通函数包成 `PluginSpec`；一个带 `inject` 属性的可调用对象会被直接识别；`Service` 的子类则通过 `_as_spec` 转成「构造一个实例并在构造中 `provide`」的规格。三种写法在框架眼里都是「一个有待加载的插件」。

## 3. 上下文是服务容器

一个服务占据一个稳定的键，比如 `tools`、`llm`、`agents`。`ctx.tools` 不是一个全局变量，而是「当前上下文能看到的 `tools` 服务」。

容器提供了三种操作。

**发布服务。** 插件通过 `ctx.provide(key, value)` 或 `Service` 基类注册一个服务，并且**只能注册自己拥有的服务**。重复注册同一个键会直接报错，教学实验里抛 `SERVICE_EXISTS`。

**查找服务。** `ctx.get(key)` 返回当前可见的实现。实验里的查找规则是：

```python
def lookup(self, key):
    entry = self._registry.get(key)
    if entry is None:
        return None
    context, value = entry
    if context.fiber.state != ACTIVE:
        return None
    return value
```

注意最后两行：**只有提供方仍处于活动状态，服务才可见。** 这条规则把「服务存在」和「服务可用」统一成一个判断。真实 Cordis 的 `strict` 模式（默认开启）就是这个意思——只返回提供方 fiber 当前处于活动状态的实现。

**声明依赖。** 插件用 `inject` 声明自己需要哪些服务：

```python
@plugin(name="reporter", inject=("metrics",))
def reporter(ctx, config):
    ctx.get("metrics")["records"].append("started")
```

框架保证：`apply` 被调用时，`inject` 里的服务一定已经就绪。如果还没就绪，插件就**等着**，不会执行。这就是为什么 `apply` 里可以放心地直接使用 `ctx.metrics`，而不需要写一堆 `if ctx.get(...) is not None`。

教学实验把这条规则做成了一个 watcher 列表：插件创建时，为每个注入的键登记自己；每当某个服务发布，就检查所有等待它的插件是否依赖已满足，满足则加载。真实 Cordis 内部有更复杂的注册表与 epoch 机制，但外部行为就是这句话。

**可选依赖的处理方式不同。** 如果一个服务可有可无，就不要写进 `inject`，而是在使用点查询：

```ts
export function apply(ctx: Context) {
  const metrics = ctx.get('metrics')
  metrics?.record('plugin_loaded', 1)
}
```

这样即使 `metrics` 不存在，插件也能正常加载。

## 4. 依赖驱动加载：服务消失会卸载，恢复会重载

`inject` 不只是「加载前检查一次」。它是一条**持续**的约束：服务在运行期间消失了（比如提供它的插件被替换或卸载），依赖它的插件会被自动卸载；等服务重新出现，它会被自动重新加载。

教学实验用两条规则表达这个循环：

```python
def service_removed(self, key):
    for context in list(self._watchers.get(key, ())):
        if context.fiber.state == ACTIVE and key in context.inject:
            context.unload()      # ACTIVE -> PENDING，等它回来

def service_added(self, key):
    for context in list(self._watchers.get(key, ())):
        if context.fiber.state == PENDING and context.dependencies_ready():
            context.load()        # PENDING -> ACTIVE，重新 apply
```

为什么要这么绕，不干脆让插件在被调用时自己处理「服务没了」？因为**自动卸载 + 自动重载**能保证一个不变量：一个处于活动状态的插件，它声明的每个依赖都一定可用。插件作者因此不必在业务代码里到处写防御性判断，恢复逻辑也只写一遍。

这也是 HMR（热模块替换）能工作的基础。修改插件代码时，框架做的是「卸载旧实例 → 用新代码加载」。因为服务消失触发卸载、服务恢复触发重载都是同一条路径，热替换和正常运行走的是同一套生命周期代码。

**服务隔离**是这套机制的一个变体。默认情况下一个键对应一个实现；通过 `cordis-plugin-group` 的 `isolate`，可以让不同的插件组各自看到自己的实例：

```yaml
- id: group-a
  name: '@deepseek-ai/cordis-plugin-group'
  group: true
  isolate:
    shell: true
  config:
    - name: '@deepseek-ai/dsh-bash-local'
      config:
        timeoutMs: 5000
```

这样 group-a 和 group-b 各自持有自己的 shell 实例，互不干扰。教学实验没有实现隔离，它在同一棵上下文树里共用一个注册表。

## 5. 事件：五种分发模式

服务和事件是 Cordis 的两条通信路径，分工不同：**服务用于直接能力调用**（比如「用这个工具」），**事件用于拦截和策略**（比如「在工具执行前允许别人改写这个调用」）。

事件名遵循 `namespace/action` 的命名约定，例如 `agent/pre-step`、`agent/request-error`、`tools/result`、`session/event`。命名不是装饰：看到 `namespace/action` 就能猜出它属于哪个子系统、在哪个阶段触发。

一个事件只有**一种**分发模式，由它的注册方式决定。五种模式的区别可以用一张表说清：

| 模式 | 是否等待 | 顺序 | 有返回值吗 |
|---|---|---|---|
| `emit` | 否 | 按注册顺序广播 | 无 |
| `bail` | 否 | 按注册顺序，遇到第一个决定性返回值就停 | 有 |
| `serial` | 是 | 按注册顺序依次等待，第一个决定性返回值终止 | 有 |
| `parallel` | 是 | 所有监听器并行 | 无（收集结果） |
| `waterfall` | 视监听器 | 环绕式中间件 | 有 |

教学实验里它们各自是一个函数：

```python
def emit(self, event, *args):        # 广播，忽略返回值
def bail(self, event, *args):        # 首个非空值短路
def serial(self, event, *args):      # 顺序等待，首个非空值终止
def parallel(self, event, *args):    # 收集所有结果
def waterfall(self, event, value, fallback, *args):  # 环绕中间件
```

`bail` 和 `serial` 的「决定性返回值」指的是「不是 `null`、`false`、`undefined` 的值」。返回空值表示「我没有意见，交给下一个监听器」；返回一个真值表示「我拍板了」。

`waterfall` 是最需要小心的一种。它不只是一个有序列表，而是**环绕中间件**，监听器签名里多一个 `next`：

```python
def waterfall(self, event, value, fallback, *args):
    chain = self.listeners(event)

    def step(index):
        if index >= len(chain):
            return fallback()          # 链条末端：调用方提供的默认结果
        return chain[index](value, *args, lambda: step(index + 1))

    return step(0)
```

规则只有一句：**监听器必须调用 `next()` 才能把请求交给下游。** 不调用 `next()` 直接返回，就是短路，下游监听器根本不会运行。这是刻意设计的：策略插件在自己的职责范围内拍板时短路，只做观察或标注的插件必须老老实实委托。

实验用一个例子展示两层包装：

```python
bus.on("transform", lambda value, next_: "[" + next_() + "]")
bus.on("transform", lambda value, next_: next_() + "!")
bus.waterfall("transform", "x", lambda: "base")   # 结果："[base!]"
```

内层先加 `!`，外层再套方括号。如果第一个监听器直接返回 `"replaced"` 不调用 `next()`，结果就是 `"replaced"`，第二个监听器不会执行。

`emit` 和 `bail` 不等待异步结果，`serial` 和 `parallel` 会等待。教学实验是同步模型，`parallel` 用顺序调用模拟「结果形状」而不模拟真实并发时序——这一点在正文里必须说清楚，不能假装测到了并发。

### 5.1 Cordis 事件不等于会话事件

这是初学者最容易混淆的一处。Harness 里有两套名字很像的东西：

- **Cordis 事件**：`agent/pre-step`、`agent/request-error`、`tools/result`、`session/event` 等，是插件间在**运行时**通信的钩子。
- **会话事件**：`turn/*`、`step/*`、`tool/call`、`tool/result`、`compaction/*` 等，是**持久化**在会话日志里、用于重建状态和回放的记录。

它们不是同一批东西，名字也不一定重名。想观察「工具调用完成」这件事，你有两种合法做法：监听 Cordis 事件 `tools/result`，或者监听 `session/event` 再检查 `event.type == 'tool/result'`。前者是运行时钩子，插件卸载就没了；后者来自持久日志，即使当时没有监听器，事后也能从日志里读出来。第五讲讲的压缩、第四讲讲的状态重建，都是建立在会话事件之上的。

## 6. 注册是可逆副作用

Cordis 有一条贯穿始终的约定：**通过 `ctx` 做的任何注册，都是可逆副作用，插件卸载时自动撤销。**

框架自动追踪的注册包括：

- `ctx.on(event, handler)` —— 事件监听器；
- `ctx.tools.register(tool)` —— 工具注册；
- `ctx.llm.registerAdapter(names, adapter)` —— 模型适配器注册；
- `ctx.effect(() => cleanup)` —— 任意自定义资源。

前三者是框架提供的便捷方法，内部都走 `effect`。第四种给插件作者留了口子：需要管理定时器、连接、文件句柄时，把清理函数交出来。

```python
def effect(self, execute, label="effect"):
    if self.fiber.state in (UNLOADING, DISPOSED):
        raise CordisError("INACTIVE_EFFECT", "cannot create effect on inactive context")
    disposer = execute()
    self.fiber.add_effect(label, disposer)
    return disposer
```

`execute` 立即运行，它返回的清理函数被收进 fiber；卸载时按**注册顺序的逆序**执行。实验里注册 `first`、`second` 两个 effect，卸载时输出顺序是 `second`、`first`：

```python
plugin_ctx.effect(effect_that_disposes("first"), "first")
plugin_ctx.effect(effect_that_disposes("second"), "second")
plugin_ctx.dispose()
# order == ["second", "first"]
```

逆序的理由很直接：后面注册的东西可能依赖前面注册的东西，所以要先把后面拆掉。比如先加监听器、再启动定时器去调用它，卸载时应该先停定时器，再摘监听器。

**但逆序只保证「开始」的先后。** 真实框架里清理函数可以是异步的，多个异步清理器会**并发**开始，不保证逐个完成。所以如果两步清理之间有顺序依赖，必须把它们放进**同一个** `ctx.effect()` 返回的清理器里，由那个清理器负责串行等待。这是文档里反复强调的一条实践规则，也是教学实验刻意简化掉的地方：实验里的清理器都是同步的，所以看起来严格逆序。

对已经 dispose 的上下文再注册 effect，会抛 `INACTIVE_EFFECT`。这条校验的意义是：与其让一个「幽灵注册」挂在已被释放的对象上，不如立刻失败。

## 7. 子插件与生命周期状态机

`ctx.plugin(childPlugin)` 创建一个**子 fiber**。它继承父上下文（因此能看到父级可见的服务），但有独立的生命周期：父插件卸载时，子插件被递归卸载。

```python
def plugin(self, plugin, config=None):
    spec = _as_spec(plugin)
    child = Context(self.bus, name=spec.name, parent=self)
    child.spec = spec
    child.inject = tuple(spec.inject)
    ...
    self.fiber.children.append(child)
    for key in child.inject:
        self.bus.watch(key, child)
    if child.dependencies_ready():
        child.load()
    return child
```

每个插件实例对应一个 **Fiber**，状态转换是：

```
PENDING → LOADING → ACTIVE
                 ↘ FAILED
ACTIVE → UNLOADING → DISPOSED
```

| 状态 | 含义 |
|---|---|
| `PENDING` | 已声明，但所需依赖尚未就绪 |
| `LOADING` | 依赖就绪，正在执行 `apply` |
| `ACTIVE` | 插件运行中 |
| `FAILED` | `apply`（或配置校验）抛出异常 |
| `UNLOADING` | 正在卸载并释放资源 |
| `DISPOSED` | 已完全卸载 |

把状态画出来之后，几个容易混淆的问题就有了统一答案：

- 「插件为什么没生效？」——先看它是不是卡在 `PENDING`（依赖缺失）或 `FAILED`（`apply` 报错）；
- 「服务为什么查不到？」——看提供方是不是处于 `ACTIVE`，见第 3 节的 `lookup`；
- 「卸载后为什么还有残留？」——检查是不是有资源没有走 `ctx.effect()`，绕过框架自己注册了。

实验里 `dispose()` 的过程是：先递归 dispose 子插件，再逆序运行自己的清理器，最后置为 `DISPOSED` 并从 watcher 列表里摘掉自己。真实 Cordis 还维护 `inertia`（正在进行的加载/卸载转换）和 `epoch`（用于隔离过期注册），并在状态转换时发出 `internal/status`，教学版本省略了这些。

## 8. 配置与热替换

插件可以接受配置。在 Harness 里，配置不是随手传一个对象，而是**用 schema 声明**：

```ts
export interface Config {
  greeting: string
  maxRetries: number
  verbose?: boolean
}

export const Config = Schema.object({
  greeting: Schema.string().default('Hello'),
  maxRetries: Schema.number().default(3),
  verbose: Schema.boolean().default(false),
})
```

加载时框架用 schema 校验配置、填入未提供的默认值。**配置错误要响亮**：无效的配置让插件加载失败并给出明确信息，而不是带着半个默认值默默运行。

教学实验用一小段 schema 模拟同样的行为：

```python
schema = {"greeting": field(str, "Hello"), "maxRetries": field(int, 3)}
```

`validate_config` 做三件事：检查类型、填入默认值、拒绝未知键。`field(str, "Hello")` 表示可选字段加默认值；`field(str)` 不给默认值，就变成必填字段。

有一条 Harness 明文约定的设计原则：**凡是不同部署可能需要取不同值的参数，都必须定义为配置字段，不能硬编码。** 判断标准很简单——「能不能只改 `cordis.yml`、不改代码，就让这个值变掉？」

配置变化会触发**热替换**：卸载旧实例、用新配置加载新实例。由于注册都是 effect 并会自动清理，替换后不会残留旧实例的注册。实验里 `update_config` 走的就是这条路：

```python
def update_config(self, new_raw):
    validated = validate_config(self.spec.config_schema or {}, new_raw)

    def restart():
        self.raw_config = dict(new_raw)
        self.unload()
        self.load()
        return "restarted"

    return self.bus.waterfall("internal/update", validated, restart)
```

注意两点。第一，**先校验，后重启**：校验不通过时旧实例原封不动。第二，重启要经过 `internal/update` waterfall，所以其他插件（比如 HMR 本身、或者持久化插件）可以观察、改写甚至否决这次重启。实验里的 `test_update_waterfall_can_veto_restart` 就注册了一个不调用 `next()` 的监听器，把重启拦了下来。

## 9. 对照 DeepSeek Harness 源码

本讲涉及的机制可以在固定版本里逐条核对。源码链接都指向本轮学习使用的提交 `5badb150`：

- [Cordis 入门：五个核心概念与分发模式](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/cordis-primer.zh.md)
- [插件与生命周期：Fiber 状态机](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/user/develop/framework/index.zh.md)
- [服务与依赖注入](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/user/develop/framework/service.zh.md)
- [事件系统：五种分发模式](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/user/develop/framework/events.zh.md)
- [第一个插件：函数、对象、类三种形态](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/user/develop/basic/index.zh.md)
- [插件配置与 schema](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/user/develop/basic/config.zh.md)
- [Fiber 与 effect 的完整 API](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/cordis-api/fiber.zh.md)

重点看：

- `PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED` 的转换条件；
- `inject` 声明依赖，服务消失时自动卸载、恢复时自动重载；
- `emit`、`bail`、`serial`、`parallel`、`waterfall` 的区别，尤其是 waterfall 必须调用 `next()`；
- `ctx.on()`、`ctx.tools.register()`、`ctx.llm.registerAdapter()`、`ctx.effect()` 都会自动清理；
- 清理器逆序开始，但异步清理可能并发；顺序依赖要放进同一个 effect；
- 命名约定 `namespace/action`，以及 Cordis 事件与会话事件的区别。

## 10. 思考与练习

1. 为什么插件不直接 `import` 另一个插件，而要通过 `ctx` 查找服务？
2. `inject` 声明的是「加载时检查一次」还是「持续约束」？服务中途消失会怎样？
3. `ctx.get('metrics')` 返回 `None` 可能有哪些原因？至少说出两种。
4. `bail` 和 `serial` 的差别是什么？各自适合什么场景？
5. `waterfall` 监听器不调用 `next()` 会发生什么？这为什么是「设计意图」而不是 bug？
6. 为什么清理器要逆序执行，但又不能依赖严格逆序等待？
7. 服务提供的 `lookup` 为什么要检查提供方的 fiber 状态，而不是只看键是否存在？
8. 「配置错误要响亮」和「给每个参数都配上安全的默认值」矛盾吗？
9. `internal/update` waterfall 允许其他插件否决重启，这带来了什么能力、又带来了什么风险？

<details>
<summary>自查要点</summary>

1. 通过 `ctx` 查找让实现可以替换、依赖可以声明、生命周期可以管理；直接 import 会把实现、加载顺序和清理责任全部焊死。
2. 是持续约束。服务消失时依赖插件被自动卸载（回到 PENDING），服务恢复后自动重新加载，保证活动插件的依赖始终可用。
3. 服务从未注册；服务已注册但提供方 fiber 不在 ACTIVE（正在加载、正在卸载、已卸载）；键拼写错误；或者插件处于隔离组看不到它。
4. `bail` 同步短路，适合「快速决策」；`serial` 等待异步结果并短路，适合「需要按顺序完成异步步骤、遇到决定性结果即停」。
5. 下游监听器不会执行，整条链被短路。这是刻意设计的拦截能力：策略插件拥有决策权时可以直接拍板。
6. 逆序保证「后注册的先开始清理」，符合依赖关系；但异步清理并发开始、不保证逐个完成，所以有顺序依赖时要把它们放进同一个 effect。
7. 因为「键存在」和「服务可用」是两回事。提供方正在加载或已卸载时，它的服务不应该被使用，否则会出现调用一个半初始化或已释放的对象。
8. 不矛盾。schema 提供的是「用户没写时的合理默认」，而 schema 校验保证「用户写了就必须合法」。响亮失败针对的是显式无效配置，不是缺省值。
9. 能力是：HMR、配置持久化、灰度、审计等可以在重启前后介入。风险是：一个不调用 `next()` 的监听器会静默阻止所有后续重启逻辑，这类拦截必须被清楚记录和测试。

</details>

## 11. 当前实现范围

本讲的离线实验 `examples/cordis_kernel.py` 演示：

- 上下文作为服务容器，`provide`、`get`、`inject` 三件套；
- 只有提供方处于 `ACTIVE` 时服务才可见；
- 依赖未就绪时插件停在 `PENDING`，服务出现后自动加载；
- 服务消失时依赖插件自动卸载，服务恢复后自动重载；
- `emit`、`bail`、`serial`、`parallel`、`waterfall` 五种分发模式；
- `prepend` 改变监听器顺序；
- `ctx.effect()` 收集清理器并在卸载时逆序执行；
- `ctx.on()` 注册的监听器随插件卸载自动移除；
- 子插件随父插件递归卸载；
- schema 校验、默认值填充与未知键拒绝；
- `update_config` 经 `internal/update` waterfall 重启，并可被否决。

它没有实现真实并发、异步清理、服务隔离、`internal/status` 与 epoch、真实的 HMR 文件监听、Standard Schema 互操作或任何网络与模型调用。`parallel` 只保留结果形状，不测量真实并发时序。

2026-10-16 已在 Windows、Python 3.13.2 下运行本讲示例，并通过 22 项行为测试。文档中的代码块和本地章节链接也经过检查。本轮未运行在线模型，也未启动 DeepSeek Harness。

下一讲讨论 Harness 装配：profile、bundle、preset 分别是什么，多个配置层怎样按顺序合并成一个可启动的应用。

[返回学习路线](README.md) · [上一讲](06-reliable-loop.md)