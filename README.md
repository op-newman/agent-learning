# 从零理解 Agent：原理、实现与 DeepSeek Harness 源码

这是一份随讨论逐步补充的学习笔记。目标是理解 Agent 为什么这样设计，能够写出自己的小型 Agent，并进一步读懂和扩展 DeepSeek Harness。

学习顺序按概念依赖安排。这里的“最底层”从开发 Agent 必须掌握的模型调用、消息与程序执行开始；涉及模型推理时解释必要的 token 和上下文概念，模型训练和 Transformer 数学可以另开支线。

## 使用方法

每讲沿着同一条路径推进：提出一个具体任务，观察简单实现遇到的问题，再引入解决问题的机制，最后对应仓库源码。术语随问题出现，不要求先记住整个框架。

先读正文，再运行可选实验，然后用自己的话回答文末问题。后续讨论会根据你的回答调整深度。读过的内容与已经掌握的内容分别记录。

在 GitHub 上打开本页后，点击下方学习路线中的章节即可阅读。章节中的流程图和折叠自查答案可以直接查看，源码链接固定到本轮学习使用的版本。后续章节会继续加入同一个目录。

需要运行实验时，下载或克隆这个学习笔记仓库，在仓库根目录执行：

```shell
python examples/01-minimal-loop.py
```

## 学习路线

| 阶段 | 核心问题 | 预期能做什么 | 进度 |
|---|---|---|---|
| 01 模型与最小循环 | 模型怎样通过普通程序完成一项任务？ | 解释消息、工具、循环、状态和停止条件 | [第一讲已整理](01-model-tools-and-loop.md) |
| 02 真正连接模型 | 本地数据怎样变成 API 请求和响应？ | 编写真实 API 客户端，理解协议适配与联调 | [第二讲已整理](02-real-model-api.md) |
| 03 工具执行器 | 怎样让工具调用可预测、可控、可排错？ | 实现注册、校验、权限、错误结果和协作式停止 | [第三讲已整理](03-tool-executor.md) |
| 04 会话与状态 | 为什么不能只保留屏幕上的聊天文字？ | 保存事件，重建消息，理解恢复和副作用问题 | [第四讲已整理](04-session-and-state.md) |
| 05 上下文管理 | 信息越来越多，怎样仍然让模型做对事？ | 理解上下文选择、工作区指令、技能和压缩 | [第五讲已整理](05-context-management.md) |
| 06 可靠的循环 | 怎样处理流式输出、重试、取消和并发？ | 画出状态转换，处理未完成的工具调用 | [第六讲已整理](06-reliable-loop.md) |
| 07 Cordis 插件基础 | 模块怎样相互使用又能独立替换？ | 理解 Context、Service、依赖注入、事件、卸载 | [第七讲已整理](07-cordis-plugins.md) |
| 08 Harness 装配 | 插件怎样组成不同的应用？ | 理解 profile、bundle、preset 和配置覆盖 | [第八讲已整理](08-harness-assembly.md) |
| 09 多 Agent 与工作流 | 什么时候值得委派，怎样协作和收尾？ | 实现一个有明确输入输出的子任务 | [第九讲已整理](09-multi-agent-and-workflows.md) |
| 10 独立开发与评估 | 怎样证明自己的 Agent 在目标任务上有用？ | 开发一个插件或应用，用任务样例验证行为 | [第十讲已整理](10-development-and-evaluation.md) |

路线可以随理解程度调整。每次讨论优先解决当前层的问题。

## 当前材料

- [第一讲：从一次模型调用到最小 Agent](01-model-tools-and-loop.md)
- [第二讲：把模拟模型换成真实模型 API](02-real-model-api.md)
- [第三讲：工具执行器怎样把请求变成行动](03-tool-executor.md)
- [第四讲：会话日志、状态重建与崩溃恢复](04-session-and-state.md)
- [第五讲：上下文管理怎样决定模型看见什么](05-context-management.md)
- [第六讲：可靠的循环](06-reliable-loop.md)
- [第七讲：Cordis 插件基础](07-cordis-plugins.md)
- [第八讲：Harness 装配](08-harness-assembly.md)
- [第九讲：多 Agent 与工作流](09-multi-agent-and-workflows.md)
- [第十讲：独立开发与评估](10-development-and-evaluation.md)
- [可运行实验：最小循环](examples/01-minimal-loop.py)
- [真实 API 客户端：默认预览请求，--live 联网](examples/chat_completions_agent.py)
- [本地 HTTP 协议测试](tests/test_real_api_protocol.py)
- [工具执行器实验：离线运行八种场景](examples/tool_runtime.py)
- [工具权限、参数和取消行为测试](tests/test_tool_runtime.py)
- [会话日志实验：日志、投影与崩溃修复](examples/session_log.py)
- [会话恢复和持久前缀行为测试](tests/test_session_log.py)
- [上下文预算实验：指令、技能目录与压缩](examples/context_budget.py)
- [上下文装配与压缩行为测试](tests/test_context_budget.py)
- [可靠循环实验：流式、重试、取消与并发](examples/reliable_loop.py)
- [可靠循环行为测试](tests/test_reliable_loop.py)
- [Cordis 内核实验：服务、依赖与事件](examples/cordis_kernel.py)
- [Cordis 内核行为测试](tests/test_cordis_kernel.py)
- [装配层实验：profile、bundle 与 patch](examples/assembly_layers.py)
- [装配层行为测试](tests/test_assembly_layers.py)
- [多 Agent 与工作流实验：委派、任务板与工作流](examples/agent_team.py)
- [多 Agent 与工作流行为测试](tests/test_agent_team.py)
- [评估实验：录制回放、轨迹与副作用断言](examples/evaluation_harness.py)
- [评估行为测试](tests/test_evaluation_harness.py)
- [实验读取的示例文件](examples/fixture/package.json)

示例只使用 Python 标准库。第一讲用固定规则的模拟模型展示数据流；第二讲默认预览请求，显式使用 --live 时才调用配置的模型 API；第三讲离线运行工具执行场景；第四讲用内存持久模型演示事件日志、消息投影和崩溃修复；第五讲离线演示工作区指令预算、技能目录按需加载和历史压缩投影。第六讲离线演示流式事件、重试、取消与并发；第七讲用小型内核演示 Cordis 式服务、依赖和事件；第八讲演示 profile、bundle 与 patch 的分层装配；第九讲演示委派、任务板和工作流；第十讲演示录制回放、轨迹检查与副作用断言。本地 HTTP 测试不连接在线模型。模拟响应和程序测试不能验证真实模型的判断能力。

## 源码基线

仓库：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)。

本轮学习使用的检出提交：`5badb15009ae1756c3afe0ae0cef1faafc290ccc`，根包版本为 `0.2.1-alpha.1`。学习笔记建立于 2026-10-09。

仓库代码会变化。正文优先引用路径和函数名；固定提交链接用于复核历史版本。教学示例和伪代码会明确标注，不能直接当作该项目的 SDK 调用方式。

## 学习进度

前十讲已整理。可以依次观察模型请求、工具执行、结果回传、事件日志、上下文预算、可靠循环、插件内核、分层装配、多 Agent 协作和独立评估，并用各讲的离线实验验证关键行为。
