# 第八讲：Harness 装配——profile、bundle、配置层与覆盖

[返回学习路线](README.md) · [上一讲：Cordis 插件基础](07-cordis-plugins.md)

第七讲回答的是「一个插件怎样被加载」：上下文是服务容器，依赖通过 `inject` 声明，副作用可以被撤销。但一个真正能启动的 `dsh` 不是从一份手写的配置文件里长出来的。同一份安装，`dsh --profile web` 得到浏览器界面，`dsh --profile headless` 得到无界面运行器，`dsh --profile sdk-minimal` 得到一个几乎空白的编程代理——它们共用大量插件包，却有着不同的能力表和不同的默认值。

这些差异不是靠「运行时读一个开关」实现的，而是靠**启动前把若干层配置叠在一起**。这一讲回答三个问题：profile、bundle、preset 各自是什么；四层配置按什么顺序合并；为什么一条看起来只是「改个模型名」的 patch 会让整个 profile 拒绝启动。

配套实验 `examples/assembly_layers.py` 用纯 Python 实现了一个几十行的装配模拟器，复现了层顺序、按 id 的整块替换、平台门控和服务重名失败。它不加载任何真实插件，也不读写真实文件。

## 1. 同一份代码，几个应用：profile 是什么

先看一个具体的困惑。你在自己的机器上装了一份 `dsh`，它同时能做几件看起来差别很大的事：

- `dsh --profile web` 启动一个带图形界面的会话；
- `dsh --profile headless` 在不带界面的环境里跑一个任务；
- `dsh --profile sdk` 让 Python 进程通过 JSON-RPC 驱动一个完整代理；
- `dsh --profile sdk-minimal` 只发布一个 shell 工具，把会话写成未压缩 JSONL。

这四种形态共享同一批包：`@deepseek-ai/dsh-tools`、`@deepseek-ai/dsh-llm`、`@deepseek-ai/dsh-session`……如果它们各自维护一份完整的插件清单，任何一次公共改动都要同步四遍。真实的做法是反过来：**公共部分只写一遍，差异部分各自补一层。**

于是有了两个概念：

> **bundle（组合包）**是一层可以分发的配置差异，它本身不是一个能运行的应用。
>
> **profile（装配）**是一个命名的应用装配，它按顺序列出若干 bundle，再叠加自己的差异。

可以把 bundle 想成「一份补丁文件」，把 profile 想成「一份补丁的清单 + 最后一道自己的补丁」。真正启动的，是这两样东西叠出来的那棵树。

## 2. bundle：可分发的一层 patch

bundle 是一个普通的 npm 包，它的 `package.json` 里有这样一段：

```json
{
  "dsh": {
    "bundle": {
      "patch": [
        "./cordis.patch.yml",
        "./presets/standard.patch.yml",
        "./presets/ptc.patch.yml"
      ]
    }
  }
}
```

`dsh.bundle.patch` 是一个**有序的文件列表**。启动器按这个顺序读取 patch 文件，把它们的内容依次追加到当前装配的末尾。注意用词：这里没有「继承」，只有「在后面再叠一层」。列表里的第一份 patch 通常负责插入这个 bundle 提供的所有行，后面的 patch 再按 id 修改其中某些行。

patch 文件的语法就是 Cordis 的 entry-list YAML，它有两种条目：

```yaml
# 插入新行：这些 id 之前不存在。
- insert:
    - id: tool-str-replace-editor
      name: '@deepseek-ai/dsh-tool-str-replace-editor'
      config:
        maxOutputChars: 16000

# 按 id 修改已有行：这一行必须已经存在。
- id: agent-default-model
  config:
    provider: deepseek-official
    model: deepseek-pro
```

`insert` 负责「增加」，带 `id` 的条目负责「覆盖」。两者的判定条件相反，这一点后面会反复用到：`insert` 要求 id **不存在**，按 id 修改要求 id **已经存在**。弄错方向就会得到 `DUPLICATE_ENTRY` 或 `UNKNOWN_ENTRY` 这类加载错误。

`dsh-base` 就是这样一份 bundle。它的 `cordis.patch.yml` 是「一个应用在空 profile 根之上的一次 insert」，内容涵盖模型连接、工具集、持久会话和安全默认值。文件里的大段注释不是装饰，它们记录的是每一行为什么在这里，以及它属于哪个层——因为后面每一个覆盖它的人都要先读懂这些理由。

## 3. profile 里到底有什么

profile 是一个目录，位于 `$DSH_HOME/profiles/<name>`。它有自己的 `package.json` 和 `cordis.patch.yml`。创建一个基于共享核心的最小 profile，只需要声明自己依赖哪些 bundle：

```json
{
  "name": "my-profile",
  "private": true,
  "dsh": {
    "profile": {
      "bundles": ["@deepseek-ai/dsh-base"]
    }
  }
}
```

`dsh.profile.bundles` 同样是**有序**的。把这个文件放进 `$DSH_HOME/profiles/my-profile/`，运行 `dsh --profile my-profile "your task"`，你就得到一个带模型访问、工具、持久化和默认权限策略的代理——因为第一层 `dsh-base` 已经把公共部分补上了。

随发行版交付的 `web`、`headless`、`sdk`、`acp` 四个 profile 都遵循同一个套路：把 `@deepseek-ai/dsh-base` 放在 bundles 的第一位，再叠加一个模式自己的 bundle（例如 `@deepseek-ai/dsh-web-app`）。它们的差异集中在模式层，公共能力留在 base 层。要添加自己的组合包，而不是改 base：

```shell
dsh plugin --profile my-profile add <package>
```

这条命令维护的是 profile 自己的依赖清单和 bundle 列表，不会去动 base 那份 patch——base 是别人发布的、会被升级的，往里面塞私货迟早会冲突。

## 4. 四层叠加顺序

现在把 patch 的来源汇总一下。一个 profile 从空白根开始，按下面的顺序叠：

| 顺序 | 层 | 来源 |
|---|---|---|
| 1 | profile 列出的每个 bundle，按 `bundles` 顺序 | 各 bundle 的 `dsh.bundle.patch` 文件列表 |
| 2 | profile 自己的 patch | `$DSH_HOME/profiles/<name>/cordis.patch.yml` |
| 3 | home 级 patch | `$DSH_HOME/cordis.patch.yml` |
| 4 | 每次调用的 overlay，按 argv 顺序 | 命令行上的每个 `--patch <path>` |

规则只有一条，但很硬：**每一行以最后一次写入为准。** 也就是说，如果 profile 的 patch 和 home 的 patch 都改了 `agent-default-model` 的 `config`，home 赢；如果命令行又给了两个 `--patch`，那么第二个文件里的那一行赢。

这条规则解释了几个日常现象。为什么把「我常用的模型」写进 home 级 patch 能覆盖随发行版的默认值？因为 home 在 profile 之后。为什么临时调试可以用 `--patch`？因为它在最后一层，不需要改动任何持久文件。

还有一条容易忽略的性质：**行之间的顺序不决定加载顺序。** patch 文件的书写顺序（对同一行来说）决定谁覆盖谁，但最终哪一行先激活、哪一行后激活，是由「服务是否可用」驱动的，而不是由它在 YAML 里排前面还是后面决定。你在 base 的 `cordis.patch.yml` 里看到的分组注释，是给读代码的人看的，不是加载语义。

## 5. patch 按 id 定位，替换整个 config

这是整讲最容易踩坑的一条，值得单独拿出来讲。

假设 base 里有这样一行：

```yaml
- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: deepseek-official
    model: deepseek-flash
```

现在你想把默认模型换成 `deepseek-pro`，于是写：

```yaml
- id: agent-default-model
  config:
    model: deepseek-pro
```

很多人的第一反应是：这应该像对象合并一样，只改 `model`，保留 `provider`。**不会。** patch 按 id 命中一行后，是**替换目标行的整个 `config`**，而不是深度合并进它。上面这段的结果是：

```yaml
config:
  model: deepseek-pro      # provider 字段消失了
```

`provider` 没了意味着什么？它意味着这个适配器的提供方落回了它自己的默认值，可能仍然能跑，也可能选到一个你没有配密钥的提供方。最坏的情况是「大部分时候看起来正常，偶尔报一个与模型名毫无关系的鉴权错误」——因为真正出问题的是那个被悄悄删掉的字段。

正确写法是把想保留的字段全部重述一遍：

```yaml
- id: agent-default-model
  config:
    provider: deepseek-official
    model: deepseek-pro
```

官方文档把这件事写成一句警告：「每个 patch 条目会替换目标的整个配置，因此请重述每个想保留的设置。」这句话不是风格建议，而是这条规则唯一安全的用法。教学实验里，`Row.restate` 用一行 `self.config = dict(entry["config"])` 把整块换掉，就是为了让这个行为在代码里也一眼可见——如果它写成了递归合并，那才是错的。

顺带说一句设计动机：为什么不做深度合并？因为「合并」在数组和嵌套对象上没有唯一正确的答案，而且会让「这一行最终是什么」变得难以预测和难以审计。整块替换只有一个答案：最后一次写入的就是结果。代价是啰嗦，收益是确定性。

## 6. 平台门控：两个孪生行，只有一个生效

同一个 base 要同时支持 macOS、Linux 和 Windows，而 shell 工具在三者上根本不是同一个东西。base 的解法是写**两个孪生行**，各带一个平台条件：

```yaml
- id: bash-sandbox
  name: '@deepseek-ai/dsh-sandbox-local'
  disabled: !!js process.platform === 'win32'
- id: pwsh-sandbox
  name: '@deepseek-ai/dsh-sandbox-windows-acl'
  disabled: !!js process.platform !== 'win32'
```

`!!js` 是 Cordis 配置里的表达式求值标记：`disabled` 不是常量 `true`/`false`，而是一段在加载时求值的 JavaScript。在 Windows 上，第一行求值为真而第二行为假；在 macOS 和 Linux 上正好相反。**每台机器上恰好有一套 shell 栈处于活动状态。**

这套机制有两个必须记住的后果。

第一，**门控写在 `disabled` 字段上，所以「覆盖 disabled」就等于「覆盖门控」。** 一个只是想让 pwsh 行在 Linux 上也开着的用户 patch，写成 `disabled: false` 就能做到——但这样一来两套 shell 栈同时在活动状态，它们注册同一项服务，于是 profile 加载失败。真实文档因此明确要求：在 Windows 上切换 shell 行时，「必须同时禁用两个 PowerShell 行并重新启用两个 bash 行，否则 profile 无法加载」。门控不是一道闸门，它是可以被覆盖的一个普通字段。

第二，**两个孪生行不能同时是活动状态，但装配阶段通常发现不了。** 装配只看 YAML，它不会执行 `!!js` 表达式去数「现在有几个 shell」。真正的冲突发生在插件启动时——两个提供方注册同一个服务键，容器直接拒绝。所以「装配正确」和「能启动」是两件事：前者是静态结果，后者要等插件真正加载才知道。

## 7. 装配为什么会失败：重复的服务

第七讲说过，一个服务键只能有一个提供方，重复注册会直接报错。这条规则在装配层面产生了一批固定的失败模式，文档里把它们整理成了一张表：

| 出错的东西 | 后果 |
|---|---|
| 根配置或必需 overlay 缺失、不可读、格式错误 | 终止启动 |
| 某个可选插件的模块 import 失败 | 警告，继续启动 |
| 某个**必需**条目加载失败 | 终止启动 |
| 插件配置 schema 校验失败 | 可选：警告继续；必需：终止 |
| 注入的服务一直不可用 | 该条目保持等待，其余继续 |
| HTTP 端口绑定失败 | 进程继续，但该端点不可用 |
| 条目被显式禁用 | 直接忽略 |

这里「可选」和「必需」的分界不是随便定的。启动器维护一份全局 required 列表，覆盖共享的代理执行、应用端点和界面传输：`agent-loop`、`webserver`、`modules`、`connection`、`headless-runner`、`acp`、`sdk-jsonrpc-server`。这些东西缺了，应用就没有意义，所以必须让它响亮地失败。而一个可选的第三方提供方挂了，其余功能还能用，就应该降级继续——启动时保留所有能激活的插件，只给失败的条目打一条带标签的警告。

「重复注册同一个服务」属于另一类：它不是某一行自己的错，而是**两行同时在活动状态的错**。base 文档把最常见的两种写了出来：

- 在已经有一个沙箱化文件系统提供方的情况下，再挂一个普通文件系统提供方——两者都注册 `ctx.fs`，profile 直接拒绝加载；
- 同时启用两个 shell 栈——两者都注册 shell 服务，同样拒绝加载。

这两条规则的共同点值得琢磨：它不是「你不该这么配」的建议，而是**唯一性约束**。一个进程应该只有一个文件系统入口、一个 shell 入口，因为下游工具不知道往哪个上写。约束用「加载失败」表达，比用「静默选择第一个」表达要安全得多——后者会让一部分工具写进沙箱、另一部分写进真实文件系统，而用户完全看不出来。

## 8. 一个刻意的例外：sdk-minimal

前面所有 profile 的套路都是「base 加模式层」，但 `sdk-minimal` 不是。它**刻意不继承 `dsh-base`**，而是发布一整棵独立的配置树：SDK stdio 启动、JSON-RPC 服务、一个由环境配置的 DeepSeek 适配器、显式 agent 核心、按平台选择的持久 shell，以及未压缩的 JSONL 持久化。

它省略的东西和它保留的东西一样重要。它是**没有**这些的：Web、settings、托管凭据、遥测、压缩（compaction）、文件系统工具、workspace 指令、skill、jobs、subagent。它只有**一个**工具：Linux/macOS 上的持久 `bash`，或 Windows 上的持久 `pwsh`。

为什么要有这么一个东西？因为 SDK 客户端的诉求和交互式产品的诉求不一样。一个嵌入别的程序里的 coding agent 想要的是「小、显式、可预测」：它希望自己数得清有几个工具、模型看到的系统提示词是什么、会话文件长什么样。如果它建立在 `dsh-base` 之上，每次 base 增加一个能力，嵌入方就得重新评估自己的行为和 token 预算。独立树把这个变化面从「上游怎么改」变成了「我什么时候主动升级」。

这也解释了它的一个已知限制：**用户 patch 可以扩展这棵树，也可以破坏 stdout。** 因为它是完整的应用装配，profile 自定义属于受信任的组合；一个往 stdout 写普通文本的插件会直接把 JSON-RPC 的分帧搞坏。极简不等于受限，它只是把选择权交回给使用者。

## 9. preset 和它们不是一回事

学到这里很容易把 preset 和 bundle 混掉，但它们作用在不同的时间尺度和不同的粒度上。

- **bundle / profile 决定「这个进程里有哪些插件」。** 它们在启动时定下来，换一个就要换一次启动。
- **preset 决定「新建的 Agent 里有哪些子插件」。** 同一个进程里可以声明多个 preset，由会话自己选；声明会提前加载，修改只影响之后创建的 Agent，已有 Agent 保留它当初绑定的那套组合。

用官方的说法，preset 就是「在普通 Cordis YAML 里定义 Agent 的子插件列表」，例如：

```yaml
- id: agent-preset-registry
  name: '@deepseek-ai/dsh-agent-preset-registry'
  config:
    default: standard
- id: preset-standard
  name: '@deepseek-ai/dsh-agent-preset'
  config:
    id: standard
    plugins: []
```

注意 `default` 是注册表的字段，`id`/`plugins` 是每个 preset 自己的字段。一行 `@deepseek-ai/dsh-agent-preset` 的 `plugins` 决定了这个 preset 下新建的 Agent 会挂哪些工具和提示词片段。Web 界面上那个「模板 / 模式」选择器，背后就是这个注册表。

一个容易踩的细节：**preset 不是安全沙箱。** 它里面的 YAML 和插件就是 Host 代码，可以执行任何东西。它做的隔离是「作用域」（scope）意义上的隔离——不同 preset 的服务不互相串门——而不是权限意义上的隔离。

## 10. 对照源码

本轮使用的提交是 `5badb15009ae1756c3afe0ae0cef1faafc290ccc`（根包版本 `0.2.1-alpha.1`）。

- [dsh-base 组合包](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/bundle/base/README.zh.md)
- [dsh-base 的 patch 实体](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/bundle/base/cordis.patch.yml)
- [dsh-sdk-minimal 组合包](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/bundle/sdk-minimal/README.zh.md)
- [app-boot 的 profile 章节](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/boot/app-boot/README.zh.md)
- [组合包索引](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/bundle/README.zh.md)
- [Agent preset](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/preset/agent-preset/README.zh.md)
- [Agent preset 注册表](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/preset/agent-preset-registry/README.zh.md)
- [生成的 profile 组合图](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/apps/cli/composition.md)
- [扩展与发布指南](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/user/develop/basic/publish.zh.md)

重点看：

- `dsh.bundle.patch` 是有序文件列表，`dsh.profile.bundles` 也是有序列表；
- patch 按 id 命中后替换整块 `config`，`cordis.patch.yml` 里的注释反复提醒这一点；
- 平台门控写成 `disabled: !!js`，两个孪生行恰好有一个活动；
- 重复注册同一个服务会让 profile 加载失败，而不是静默择一；
- required 列表覆盖哪些条目，以及可选项失败时为什么选择继续启动；
- `sdk-minimal` 的单一 insert 就是完整应用树，它刻意不写 `dsh-base`。

## 11. 思考与练习

1. profile 和 bundle 各自「有」什么、「没有」什么？用一个类比说清它们的关系。
2. 为什么 `insert` 要求 id 不存在，而按 id 修改要求 id 已存在？弄反分别会得到哪种错误？
3. 四层 patch 的顺序是什么？为什么 home 级 patch 能覆盖随发行版的默认值？
4. 「替换整个 config」和「深度合并」各有什么优劣？官方为什么选了前者？
5. 一条把 `model` 从 `deepseek-flash` 改成 `deepseek-pro` 的 patch，为什么要连带重述 `provider`？
6. 平台门控为什么写在 `disabled` 上，而不是一个独立的 `platforms` 字段？这带来了什么风险？
7. 为什么「两个文件系统提供方」必须是加载错误，而不能是「以第一个为准」？
8. `sdk-minimal` 为什么不继承 `dsh-base`？这个决定让谁受益、代价是什么？
9. preset 和 profile 分别在什么时刻生效？为什么改 preset 不会影响已经在跑的 Agent？

<details>
<summary>自查要点</summary>

1. bundle 是「一份可以分发的补丁层」，没有启动能力；profile 是「一份补丁清单加最后一道自己的补丁」，它才是命名应用。类比：bundle 是依赖库，profile 是应用入口。
2. `insert` 是新增，id 已经存在说明装配里有两个同名行，属于 `DUPLICATE_ENTRY`；按 id 修改是覆盖，目标不存在说明这条 patch 建立在错误的假设上，属于 `UNKNOWN_ENTRY`。两者都在加载阶段响亮失败。
3. bundle（按 bundles 顺序）→ profile 的 `cordis.patch.yml` → `$DSH_HOME/cordis.patch.yml` → 每个 `--patch`（按 argv 顺序）。后写覆盖先写，home 在 profile 之后，所以能覆盖随发行版的默认值。
4. 替换确定、可审计、没有歧义，但啰嗦且容易漏字段；深度合并省字，但在数组和嵌套结构上没有唯一正确语义，且最终结果难以审计。官方选择确定性。
5. 因为 patch 会替换整个 `config`，不写 `provider` 就等于把它删掉，适配器会落回自己的默认提供方，可能引发与「改模型名」看起来无关的鉴权或路由错误。
6. 因为门控本来就是一个普通的 `disabled` 字段（值是 `!!js` 表达式），复用它可以少一套机制。风险是用户以为自己在「打开一个开关」，实际上覆盖了门控，两套 shell 同时上线导致加载失败。
7. 因为它是一个唯一性约束。静默择一会让不同的下游工具写进不同的后端（沙箱 / 真实文件系统），而调用者看不出区别。加载失败把问题暴露在配置阶段。
8. 因为嵌入方要「小、显式、可预测」，不希望上游每加一个能力就改变自己的行为和 token 预算。受益者是 SDK 使用方；代价是它与共享核心的改进完全脱钩，需要自己跟进。
9. profile 在启动时决定进程里有那些插件，改它要换一次启动；preset 在新建 Agent 时绑定，声明会提前加载，修改只影响之后创建的 Agent。已有 Agent 保留它绑定的那套组合。

</details>

## 12. 当前实现范围

本讲的离线实验 `examples/assembly_layers.py` 演示：

- bundle 是 patch 文件的有序列表，profile 是 bundle 的有序列表加自己的 patch；
- 四层顺序：bundle → profile patch → home patch → `--patch` overlay（按 argv 顺序）；
- 每一行以最后一次写入为准；
- 按 id 的 patch 替换整块 `config`，不深度合并；
- `insert` / 按 id 修改 / 未知 id / 重复 id 的四种结果；
- 平台门控让两个孪生行恰好有一个活动；
- 覆盖 `disabled` 会清掉门控，两套 shell 同时活动后由服务重名检测报错；
- 两个文件系统提供方触发 `DUPLICATE_SERVICE`；
- 缺失服务只是警告（依赖方继续等待）；
- `sdk-minimal` 是独立树，不含 base 的行；
- `dump_config` 渲染生效树并标注每行的来源层。

它没有执行 `!!js` 表达式、没有真正加载插件模块、没有做 schema 校验、没有实现 required/optional 的启动失败矩阵、没有读取真实 npm 包与 profile 目录，也没有处理 HMR 重载。实验里的「服务重名」是静态检查，真实系统要到插件启动注册服务时才会发现。

2026-10-16 已在 Windows、Python 3.13.2 下运行本讲示例，并通过 24 项行为测试。文档中的 JSON 示例和本地章节链接也经过检查。本轮未运行在线模型，也未启动 DeepSeek Harness。

下一讲讨论多 Agent 与工作流：什么时候值得把一个任务委派出去，子 Agent 的输入输出、预算、通信和收尾应该长什么样。

[返回学习路线](README.md) · [上一讲](07-cordis-plugins.md)