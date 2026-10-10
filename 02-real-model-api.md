# 第二讲：把模拟模型换成真实模型 API

[返回学习路线](README.md) · [上一讲：模型、工具与循环](01-model-tools-and-loop.md)

第一讲已经有了一个能够运行的循环，但决定下一步的是固定规则写成的模拟函数。这一讲把 call_model() 展开：程序怎样构造请求、发送到模型服务、理解返回内容，再接回原来的循环。

我们仍然只做一个任务：读取示例 package.json，回答项目名和版本号。保持任务不变，可以集中观察“真实模型调用”这一层增加了哪些工作。

## 这一讲结束时，你应该能解释什么

- API、HTTP、JSON、SDK 分别在做什么。
- 为什么地址、模型 ID 和密钥是三项独立配置。
- 工具定义、工具调用、工具结果如何出现在网络请求里。
- 为什么 HTTP 请求成功，不等于 Agent 任务完成。
- 为什么不同供应商需要适配器，而循环可以相对稳定。

本文先用 Chat Completions 的常见文本和 function 工具调用格式进行教学。配套程序是独立实验，不是 DeepSeek Harness 的 SDK。当前检出的 Harness 直连 DeepSeek 适配器使用 Messages 协议，文末会对应两者的差异。

## 1. API 就是程序之间约定好的调用方式

上一讲的调用在同一个 Python 程序内部：

```python
response = fake_model(history, tools)
```

真实模型通常运行在另一台机器上。你的程序不能直接访问远端进程里的函数，需要按双方约定的网络格式发送请求。

API 是 Application Programming Interface，应用程序接口。这里可以把它理解为一份约定：请求发到哪里、使用什么格式、提供哪些字段、成功后返回什么、失败时如何表示。

HTTP 是传递请求和响应的协议。HTTPS 在 HTTP 传输上使用 TLS 保护连接。你用浏览器打开网页也会遇到 HTTP，但模型客户端接收的通常是结构化数据。

JSON 是一种文本数据格式，用来表示对象、数组、字符串、数字等。我们把 Python 字典转换成 JSON，才能按这个 API 的约定把数据发出去。

SDK 是供应商或社区提供的客户端代码库，可以帮你组装请求、发送 HTTP、解析结果。使用 SDK 后，调用看起来更像一个普通函数；底层仍然需要传输请求与响应。

| 名称 | 本讲中的用途 | 具体例子 |
|---|---|---|
| API | 约定调用方式 | 请求需要 model、messages 等字段 |
| HTTP / HTTPS | 传递数据 | 向一个地址发送 POST 请求 |
| JSON | 表示数据 | `{"model":"YOUR_MODEL_ID"}` |
| SDK | 封装调用细节 | 用一个库函数代替自己写 HTTP 请求 |

本讲先使用 Python 标准库直接发送 HTTP，让你看到真正发送的数据。掌握这一层后，换用 SDK 会更容易理解。

## 2. 三项配置回答三个不同的问题

调用真实模型，首先要确定三件事。

### 2.1 Endpoint：请求发到哪里

例如：

```text
https://gateway.example/v1/chat/completions
```

这是完整请求地址，也叫 endpoint。上面的域名是教学占位地址，不能用来调用真实模型。

把它拆开：

```text
https://              使用 HTTPS
gateway.example       服务所在的主机
/v1/chat/completions   API 路径
```

有些 SDK 接收 base_url，再自动补全 `/chat/completions`。本讲程序接收的是完整 endpoint，不会追加路径。两种配置方式不能混用，否则容易出现漏掉路径或重复 `/v1` 的问题。

### 2.2 Model：服务端使用哪个模型

例如：

```json
{"model": "YOUR_MODEL_ID"}
```

model 是服务端认识的模型标识。它不是本地 Python 函数名，也不是随便起一个显示名称就能使用。

同一个服务地址可以提供多个模型。同一个模型系列也可能经由不同服务地址提供。你要使用当前账号和端点实际支持的模型 ID。

### 2.3 API key：这个请求使用谁的权限和额度

本讲采用的鉴权格式是 HTTP 请求头：

```http
Authorization: Bearer <API_KEY>
```

密钥交给提供服务的系统，用于身份和权限校验。它不需要出现在 messages 中，也不应该作为提示词发给模型。

不同 API 可能要求不同的鉴权请求头。例如，当前 Harness 的 DeepSeek API-key 适配器提供的是 `x-api-key`。这也是适配器需要知道的协议差异之一。

三项配置可以这样记：地址决定“去哪里”，模型 ID 决定“用什么”，密钥决定“以谁的身份调用”。

## 3. 一次完整请求包含什么

先看请求的外层：

```http
POST /v1/chat/completions HTTP/1.1
Host: gateway.example
Content-Type: application/json
Authorization: Bearer <API_KEY>

{这里放请求正文}
```

POST 表示向这个 API 提交数据。请求头中的 Content-Type 告诉服务端：正文使用 JSON 格式。请求头和正文承担不同职责。

正文可以像这样：

```json
{
  "model": "YOUR_MODEL_ID",
  "messages": [
    {
      "role": "system",
      "content": "Read the file before answering. Use actual tool results."
    },
    {
      "role": "user",
      "content": "Read package.json and report the project name and version."
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "read_file",
        "description": "Read the teaching workspace's package.json as UTF-8 text.",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {
              "type": "string",
              "enum": ["package.json"]
            }
          },
          "required": ["path"],
          "additionalProperties": false
        }
      }
    }
  ],
  "tool_choice": "auto",
  "stream": false
}
```

先只抓住四个字段。

**messages 提供本次需要参考的信息。**本次请求里没有 package.json 的内容，所以模型尚未获得实际文件数据。

**tools 描述模型可以请求调用的函数。**这里发送的是说明和参数 schema。Python 的文件读取函数没有被上传到模型服务，它仍然在你的程序里。

**tool_choice: auto 允许模型决定是否请求工具。**它不保证模型一定读文件，也不保证一定选对工具。本讲的应用规则要求先读取，但规则是否被遵守，仍然要根据实际调用和结果观察。后续可以加入更明确的控制和验证。

**stream: false 让本次 HTTP 响应以完整结果返回。**它不改变“工具仍由客户端执行”的分工。我们先学习完整响应，第六讲再讨论流式分片。

参数 schema 里的 enum 表示允许值列表，required 表示必填字段，additionalProperties: false 表示不允许增加其他字段。即便这些要求已经发送给模型，执行工具时仍要检查实际收到的参数。

## 4. 第一次响应：服务端返回工具调用

一种符合预期的响应如下。这个 JSON 是教学样例，不是真实在线请求的记录；为了阅读省略了响应 ID、计量等字段。

```json
{
  "choices": [
    {
      "index": 0,
      "finish_reason": "tool_calls",
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "read_123",
            "type": "function",
            "function": {
              "name": "read_file",
              "arguments": "{\"path\":\"package.json\"}"
            }
          }
        ]
      }
    }
  ]
}
```

### 4.1 choices 是候选结果列表

这一套协议使用 choices 装载候选结果。本讲只支持一次返回一个候选结果，因此读取其中的第一个，并明确拒绝其他数量。

`choices[0].message` 才是本次 assistant 消息。响应外层的计量、标识和结束原因，不应直接当作聊天正文。

### 4.2 content 可以为空

当模型主要请求工具时，content 可以是 null。应用不能看到没有文字就认为响应无效，也不能只打印 content 后忽略 tool_calls。

反过来，同一个响应也可能同时有文字和工具调用。判断要不要执行工具，应该检查结构化字段。

### 4.3 finish_reason 说明这次生成为什么停下

本讲程序处理如下情况：

| 结束原因 | 程序如何处理 |
|---|---|
| tool_calls | 检查并处理对应工具调用 |
| stop | 检查是否有普通回答，结束本轮 |
| length 等其他值 | 报告异常结束，不执行可能不完整的工具请求 |

如果响应说自己因工具调用结束，却没有任何调用数据，本讲程序会报错。这样可以避免把不一致的外部数据继续传入循环。

实际 API 还可能返回内容过滤、拒绝或供应商扩展。本讲只实现清楚界定的子集；完整适配器需要分别处理支持的情况。

### 4.4 为什么 arguments 是字符串里的 JSON

观察这个字段：

```json
{
  "arguments": "{\"path\":\"package.json\"}"
}
```

这里存在两层解析：

```text
HTTP 响应正文
    ↓ 解析外层 JSON
得到 Python 字典，其中 arguments 的值仍然是字符串
    ↓ 再解析 arguments
得到 {"path": "package.json"} 这个参数对象
```

反斜杠是 JSON 字符串中双引号的转义表示，不是需要手工删掉的垃圾字符。

在 Python 中：

```python
payload = json.loads(response_text)
call = payload["choices"][0]["message"]["tool_calls"][0]
arguments = json.loads(call["function"]["arguments"])
```

外层响应是合法 JSON，不代表 arguments 内部也是合法 JSON。即使内部能解析，也不代表参数类型和取值正确。这就是为什么需要依次做协议解析、参数解析和参数校验。

## 5. 工具执行后，第二次请求怎样组织

程序成功读取文件后，先保留模型刚才的 assistant 消息，再加入一条 tool 消息。

```json
{
  "role": "tool",
  "tool_call_id": "read_123",
  "content": "{\"ok\":true,\"content\":\"{\\\"name\\\":\\\"learning-demo\\\",\\\"version\\\":\\\"0.1.0\\\"}\"}"
}
```

这一行的转义比较密集。拆开看，tool 消息的 content 是下面这个结果对象序列化后的字符串：

```json
{
  "ok": true,
  "content": "{\"name\":\"learning-demo\",\"version\":\"0.1.0\"}"
}
```

结果对象里的 content 又是文件文本。示例文件刚好也是 JSON，所以会出现多层 JSON 文本。在代码里交给 json.dumps() 和 json.loads() 完成这些转换，避免手工拼接转义字符。

其中最关键的关系是：

```text
assistant.tool_calls[0].id = read_123
tool.tool_call_id          = read_123
```

二者必须对应。模型请求多次调用时，每个结果也要保留各自的调用编号。

第二次请求的 messages 顺序变为：

```text
1. system：根据实际读取结果回答
2. user：查询项目名和版本号
3. assistant：请求 read_file，编号 read_123
4. tool：read_123 对应的执行结果
```

请求正文中的 model、tools 等字段也会按本次需要提供。只保存 history 变量还不够，构造第二次请求时要真正把它放进 messages。

第二次模型响应可能是：

```json
{
  "choices": [
    {
      "finish_reason": "stop",
      "message": {
        "role": "assistant",
        "content": "项目名是 learning-demo，版本号是 0.1.0。"
      }
    }
  ]
}
```

它也可能继续请求工具。实际下一步取决于模型和输入；上面的样例只是这一任务的预期路径。

## 6. 在 Python 中，这一层具体做了什么

配套程序：[examples/chat_completions_agent.py](examples/chat_completions_agent.py)。

建议按以下顺序阅读，先抓数据经过哪些函数，再看每个函数的错误处理：

```text
initial_history()
    创建初始规则和任务

build_request(history, model)
    把消息、工具定义和配置放进请求字典

call_model(endpoint, api_key, body, timeout)
    把字典序列化，发送 HTTP，解析响应 JSON

parse_response(payload)
    检查协议字段，取出循环可以处理的 assistant 消息

execute_tool(call)
    解析参数，读取文件，构造对应 tool 消息

run_agent(...)
    保存消息，决定继续调用还是结束
```

发送 HTTP 的核心逻辑可以单独写成：

```python
request = urllib.request.Request(
    endpoint,
    data=json.dumps(body).encode("utf-8"),
    method="POST",
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
    },
)

with opener.open(request, timeout=timeout) as response:
    payload = json.load(response)
```

这段代码里的三个转换值得注意：

1. json.dumps(body) 把 Python 字典变成 JSON 字符串。
2. encode("utf-8") 把字符串变成要发送的字节。
3. json.load(response) 从响应中读取 JSON，变回 Python 数据。

随后 parse_response() 才解释这些数据是否符合我们支持的模型响应格式。能解析成 JSON，只证明文本语法正确。

程序对普通工具错误和模型调用失败采用不同处理：工具参数写错，通常可以构造成 tool 错误结果交给模型修正；HTTP 鉴权失败或响应协议不匹配，则在调用层停止并报告问题。这样不会把所有故障都伪装成“模型的一段回答”。

## 7. 用三个层次排查一次失败

假设没有得到预期答案，可以依次检查。

### 7.1 请求是否成功抵达并被服务接受

例如：

| 现象 | 优先检查 |
|---|---|
| 连接、DNS、TLS 或超时错误 | 地址、网络、证书、代理、等待时间 |
| HTTP 400 | 请求字段、消息顺序、模型和协议是否匹配 |
| HTTP 401 / 403 | 密钥、权限、账号或服务限制；具体原因看服务端说明 |
| HTTP 404 | 完整 API 路径和资源是否正确 |
| HTTP 429 | 限流或额度情况，不能只凭状态码认定原因 |
| HTTP 5xx | 服务或网关错误 |

本讲示例保留 HTTP 状态，不自动重试。特别是密钥错误，重复发送同样的请求通常无法修复问题。

超时也不一定意味着服务端没有执行。连接断开后立即重发，可能重复产生模型计算。工具执行如果带有写入、付款等副作用，重试设计还会更加重要，后续再讲。

### 7.2 返回内容是否符合协议

HTTP 200 表示 HTTP 层面的请求成功，但仍然需要检查响应正文。

例如，错误配置的网关可能返回 HTML 页面；某个端点返回的是另一个 API 的字段；响应被截断，无法组成有效工具调用。这些都要在解析阶段识别。

### 7.3 模型是否完成了用户目标

即使 HTTP 正常、JSON 合法、响应字段正确，模型也可能没有先读文件就直接作答。

这属于任务行为问题，需要检查调用记录和实际结果。修改 API 地址或盲目重试，不能代替任务验证。

本讲代码用 turn_ended 表示“模型正常返回了一个不含工具调用的回答，本轮结束”。这个状态没有声称答案经过独立验证。

## 8. 成本、上下文和超时：先分清三个限制

模型请求通常有输入和输出计量。输入包含本次提交的历史、工具说明等；输出包含本次生成的内容。具体计费规则由供应商和模型决定。

第二次请求带上了原有历史和新工具结果，输入量通常会增加。工具在本地执行，也不意味着整个过程只有一次模型调用的成本。

先区分三个限制：

| 限制 | 管什么 | 不代表什么 |
|---|---|---|
| max_steps | 这个例子最多调用模型几次 | 不等于严格的费用或 token 上限 |
| 模型上下文窗口与输出上限 | 一次请求能容纳多少信息、生成多少内容 | 不等于一个任务能执行多少步 |
| HTTP timeout | 本次网络操作的等待行为 | 不等于整个任务的总截止时间 |

本讲的 timeout 使用 Python HTTP 客户端的超时参数，不能当作全流程的严格墙钟时间预算。

为了不把不同供应商的参数混在一起，本讲请求不强行设置统一的输出 token 字段。实际接入时应根据目标端点和模型配置输出上限；这也是当前示例尚未实现的能力。

## 9. 为什么要有模型适配器

现在假设换一个供应商，其响应不是 choices，而是 content 数组；工具调用可能放在 tool_use 内容块中，结果的表示方式也不同。

如果循环到处直接读取供应商字段，切换协议时就需要到处修改。我们可以把协议差异集中在一个组件中：

```text
应用内部的请求数据
    ↓ 适配器编码
目标 API 的请求格式
    ↓ HTTP
目标 API 的响应格式
    ↓ 适配器解码
应用内部的消息、工具调用、结束原因
```

这个组件就叫适配器。

可以把职责分成三层：

| 层次 | 主要问题 |
|---|---|
| Agent 循环 | 何时调用模型、何时执行工具、何时结束 |
| 模型适配器 | 怎样把内部数据翻译成供应商协议，再翻译回来 |
| 网络传输 | 怎样发送请求、接收响应、报告网络错误 |

这是理解上的分层，代码不一定每层都必须拆成独立文件或类。本讲把传输和解析分成函数，循环仍使用 Chat Completions 风格的消息，所以它还不是一个完整的多供应商框架。

适配器也不能抹平所有能力差异。有的模型支持图片，有的要求不同的系统提示词形式，有的需要把推理相关数据保留到后续请求。成熟框架既要统一公共部分，也要保留必要的能力信息和回放数据。

因此，本讲解析器对非空的未知 assistant 字段明确报错，避免把可能需要保留的数据默默丢掉。接入带有供应商专用推理扩展的模型时，需要实现与其匹配的适配器。

## 10. 实际运行：先看请求，再决定是否联网

本讲只使用 Python 标准库。命令都在学习笔记仓库的根目录运行。

### 10.1 不需要密钥的请求预览

```powershell
python examples/chat_completions_agent.py
```

默认只打印准备发送的请求正文，不读取密钥，也不会连接模型服务。预览里的 YOUR_MODEL_ID 是占位值。

观察两个问题：tools 里有没有真正的 Python 代码？messages 里有没有示例文件内容？初始请求中，前者只有工具说明，后者只有任务。

### 10.2 连接你选定的真实端点

先确认服务支持本讲使用的 Chat Completions 文本和 function 工具格式，再设置完整地址和实际模型 ID：

```powershell
$env:AGENT_API_URL = 'https://gateway.example/v1/chat/completions'
$env:AGENT_MODEL = 'YOUR_MODEL_ID'
python examples/chat_completions_agent.py --live
```

上面的地址和模型名都需要替换。程序会通过隐藏输入提示获取 API key；如果当前环境已设置 AGENT_API_KEY，则使用该值。示例不自动读取 .env 文件。

真实调用使用所选服务的权限和额度。程序只读取固定的教学示例文件，不提供任意文件访问或命令执行能力。

`--max-steps 4` 和 `--timeout 30` 是默认设置，可以显式传入，例如：

```powershell
python examples/chat_completions_agent.py --live --max-steps 2 --timeout 45
```

示例不会自动追加 URL 路径，也不会跟随重定向；如果服务器要求另一个地址，应根据供应商文档配置正确的 endpoint。为了限定教学范围，当前示例不接受带查询参数的 endpoint，例如需要在 URL 中指定 API 版本的某些服务，需要单独适配。

### 10.3 无真实模型的本地 HTTP 验证

```powershell
python -m unittest discover -s tests -v
```

测试在本机临时启动 HTTP 服务，返回预先构造的模型响应。它使用虚构的测试密钥，不访问在线模型，不产生模型费用。

测试真正经过 HTTP 请求、JSON 编解码、文件读取和循环，覆盖：

- 第一次请求声明工具，第二次请求带上实际文件内容，并保持调用编号一致。
- 步数上限触发时，不再发送第二次请求，也不声称已有最终答案。
- 工具参数不是合法 JSON 时，把错误作为下一次请求中的观察结果。
- HTTP 401 时报告失败，不自动重复请求。
- 生成因 length 截断时，不执行该响应中的工具请求。

**本地协议测试不能证明真实模型会正确选择工具。**它验证的是应用怎样处理事先安排好的外部响应。真正的任务表现，还需要使用选定模型和真实任务样例评估。

## 11. 对应到 DeepSeek Harness 源码

本节依据本仓库学习基线提交 `5badb15009ae1756c3afe0ae0cef1faafc290ccc`。它描述这个版本的实现，不把代码中的协议选择推广成所有 DeepSeek API 的唯一调用方式。

### 11.1 内部请求：GenerateOptions

在 `packages/llm/llm/src/types.ts` 中，GenerateOptions 定义内部模型请求，包括：

```text
provider    选择已经注册的提供方路由
model       选择该路由上的模型
messages    本次模型消息
tools       本次工具 schema
signal      取消信号
```

这份内部表示还包含其他可选字段。provider 是 Harness 的路由选择信息，不必直接成为供应商 HTTP 正文里的同名字段。

### 11.2 统一调用入口：LlmAdapter 和 ctx.llm

在 `packages/llm/llm/src/index.ts` 中，LlmAdapter 定义适配器接口，ctx.llm 管理路由和调用。Agent 循环通过服务接口使用模型能力。

当前接口还提供 prepareCall()，把一次调用所需的模型信息和后续请求发送绑定起来。这样可以避免准备请求期间配置发生变化，导致能力信息来自旧配置、请求却发往新地址。先知道它解决的这个问题即可，生命周期细节后续再读。

### 11.3 DeepSeek 直连：Messages 协议

在 `packages/llm/llm-deepseek/src/adapter.ts` 中，可以实际看到：

```text
内部 messages
    → serialize(...) 形成供应商正文
    → POST 到 /v1/messages
    → parseSse(...) 解析流式事件
    → translate(...) 转换为内部 StreamChunk
```

这里使用 Messages 协议，并请求流式结果。API-key 认证插件提供 x-api-key 请求头。它与本讲 `/chat/completions`、Authorization Bearer、choices 的教学路径不同，不能只替换 URL 后继续使用原解析器。

### 11.4 多供应商：llm-pi-ai

`packages/llm/llm-pi-ai/` 通过 pi-ai 支持多种提供方及自定义网关。配置中包括协议、端点、模型目录和兼容性选项。

这正是前面分层的实际用途：循环面向 Harness 的模型服务，具体请求格式由对应适配器处理。

固定源码入口：

- [内部请求 GenerateOptions](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm/src/types.ts)
- [LlmAdapter 与模型服务](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm/src/index.ts)
- [DeepSeek 请求发送和流式转换](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-deepseek/src/adapter.ts)
- [DeepSeek API-key 鉴权插件](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-deepseek-api-key/src/index.ts)
- [pi-ai 多提供方适配器说明](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/packages/llm/llm-pi-ai/README.md)

## 12. 三个常见误解

**“服务兼容 Chat Completions，所以任意参数都通用。”**兼容通常有范围。工具调用、推理字段、图片、输出上限和系统消息形式仍可能不同，要以目标服务支持的协议为准。

**“用了 SDK，就不用理解 HTTP 和消息结构了。”**SDK 能减少代码，但遇到路径配置、字段不支持、历史缺失和结果配对错误时，仍要理解实际发出的请求。

**“模型返回了工具调用，就说明服务器执行了工具。”**对于本讲声明的客户端 function 工具，返回值只是执行请求。实际动作仍由你的程序完成。某些服务另有服务端内置工具，那是另一种执行安排，需要按对应 API 单独理解。

## 13. 思考与小练习

1. 请求里的 tools 说明 read_file 存在，但本地程序没有实现这个函数，会发生什么？
2. 响应外层是合法 JSON，arguments 却是 `"{broken"`。应该在哪一步发现问题？如何把它交回模型？
3. HTTP 200，模型正常回答了版本号，但它完全没有调用读取工具。我们能证明答案来自当前文件吗？
4. 把 API 地址从 Chat Completions 改成 Messages，保留原来的请求和 choices 解析代码，为什么通常不能工作？
5. 在第二次请求中保留 tool 结果，却删除对应 assistant 工具调用，会丢失什么关系？

<details>
<summary>自查要点</summary>

1. 模型可以提出调用，但程序无法分派到实现。执行器应报告未知工具，或者在装配工具目录时保证声明与实现一致。
2. 外层 HTTP 响应解析可以成功；工具执行器解析 arguments 时发现它不是合法 JSON。程序可以生成带原调用 ID 的工具错误结果，让后续调用有机会修正。
3. 不能。协议成功与任务证据是不同层次。需要检查实际工具记录或通过其他明确机制提供了文件内容。
4. 两个接口对请求正文、工具表示、结果角色、鉴权或响应结构的约定可能不同，需要匹配的适配器。
5. 丢失“这个结果回应了模型哪一次请求”的关系，具体服务还可能拒绝不完整的消息序列。

</details>

## 14. 当前范围与验证记录

教学程序支持：单候选、非流式、文本输出、客户端 function 工具、Bearer 鉴权和固定文件读取。它不支持服务端内置工具、多模态、供应商推理扩展、自动重试、持久化会话或严格的全任务费用预算。

本讲没有预设一个“所有平台通用”的真实模型 ID，也没有调用付费模型服务。配套测试用于验证本地客户端的数据流，不能代替真实端点联调。

2026-10-10 已在 Python 3.13.2 下执行请求预览和 `python -m unittest discover -s tests -v`：请求预览正常，5 项本地 HTTP 测试全部通过。

协议参考入口：[OpenAI 函数调用指南](https://developers.openai.com/api/docs/guides/function-calling)、[Chat Completions API 参考](https://platform.openai.com/docs/api-reference/chat/create)。2026-10-10 本轮访问这两个官方页面均返回 HTTP 403，未能实时读取其内容；本文不据此声称最新接口已在线核验。接入真实服务时，请核对该服务当前文档。上述 Harness 源码映射已对照本地固定版本阅读。

下一讲从 execute_tool() 出发，专门讨论“工具定义怎样与实现保持一致、参数如何校验、失败如何表达、权限在哪里执行”。

[返回学习路线](README.md) · [上一讲](01-model-tools-and-loop.md)
