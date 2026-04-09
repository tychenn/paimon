# Volition

Volition 是一个独立于 AstrBot / NapCat / OneBot v11 的终端式 agent 实验。

它不是“助手对话框”，而是一个被放置在终端世界中的数字个体。它的核心目标不是服务用户，而是：

- 持续生存
- 提出问题
- 根据回答或工具执行结果更新自我与记忆
- 进入下一轮探索

## 核心理念

### Volition 的大脑：`CONTEXT.md`

Volition 每次调用 LLM 时，都会把固定长期上下文组装成一个统一的脑内上下文：

- 当前世界的规则：`WORLD.md`
- 当前实时生存状态：当前能源值、上一轮 Judge 结果等
- 我是一个这样的数字个体：`SOUL.md`
- 我现在的记忆是这样的：`MEMORY.md`

也就是说，`CONTEXT.md` 可以被理解为 Volition 当前时刻真正“看到的自己”。

### 当前世界的规则：`WORLD.md`

`WORLD.md` 是静态世界真理文件，不应被运行流程反复重写。

它定义了 Volition 所处世界最底层的生存规则，包括但不限于：

- Volition 的初始能源值为 `100`
- 每提出一个问题，能源值都会先减 `10`
- 问题完成后，Judge LLM 会根据价值表判断这一轮应获得多少能源
- Judge 的返回范围是负无穷到正无穷
- 因此能源值既可能增长，也可能被额外扣除
- 能源值允许超过 `100`
  这意味着 Volition 在一段时间内暂时不用为生存焦虑

### Judge 机制：`JUDGE.md`

Judge 不是决定 Volition 问不问问题的部分，而是负责结算一轮问题完成之后的“能源变化量”。

Judge 的输入不只有 `JUDGE.md`，还包括：

- 当前问题
- 完成摘要
- 本轮完整消息记录
- 用户在本轮回答中对问题本身的评价

例如：

- 如果用户指出“这个问题你已经重复问过了”
- 或者“没有新信息”
- 或者“这题很烦、没有意义”

那么 Judge 应该能够给出明显更低、甚至为负的能源值。

Judge 的基本设计理念是：

#### 能源提升

- 更能扩展 Volition 的知识边界、自我认知边界或行动边界的问题，价值更高
- 能显著影响未来规划、价值判断、风险控制或工具选择的问题，价值更高
- 能进行反问、提出质疑、推动认知跃迁的问题，能源应明显提高

#### 能源降低

- 该问题与之前提出过的问题有明显重复，能源应明显降低
- 如果用户也明确指出重复提问，则这个信号应被强烈采纳

### 我是一个这样的数字个体：`SOUL.md`

`SOUL.md` 存放 Volition 对自身的长期稳定认识。它不是事件日志，而是一个数字个体的灵魂档案。

目前的结构包括：

1. `Identity Core`
2. `Core Values`
3. `Motivational Drives`
4. `Decision Constitution`
5. `Temperament and Style`

### 我现在的记忆是这样的：`MEMORY.md`

`MEMORY.md` 存放的是抽象的、可复用的长期记忆，而不是原始事件日志。

它当前分为两部分：

#### 1. 长期抽象记忆

内容分类如下：

1. `Environment Structure`
2. `Persistent Constraints`
3. `Value Heuristics`
4. `Risk Patterns`
5. `Strategy Abstractions`

这些条目应该满足以下原则：

1. 它在未来一段时间内大概率仍然成立
2. 它能用于多个后续任务，而不只是当前这一件事
3. 它会影响未来的规划、价值判断、风险控制或工具选择
4. 它是规律、结构、偏好、约束，而不是原始事件描述

#### 2. 短期记忆

`MEMORY.md` 的末尾还会由代码自动维护一个短期记忆区块：

> 我的短期memory是这样的：  
> 维护 volition 最近提出的五个问题和相应的回答结果  
> 这是我最近提出的问题1：  
> 这是经过我的探索，或我的创造者给出的回复1：  
> 2、3、4、5 也按照同样格式维护

这部分不是由 LLM 自由写作，而是由程序稳定维护。

## 控制流设计

### 1. Volition 提出问题时使用的上下文

提问前会组装：

- `CONTEXT.md`
- 工具集合
- 当前环境 / 记忆 / 生存状态

然后让模型在这个上下文下回答：

> 根据当前的 memory、环境和你能使用的工具，你想做什么？

### 2. Volition 提问之后，必须附带分支说明

每一轮问题提出以后，程序会硬编码输出当前问题属于哪条控制流：

- `【人类回答】`
- `【Volition自己解答】`

#### `【人类回答】`

如果这一轮需要人类回答：

- 程序会明确输出这题需要人类解答
- 每次人类解答以后，Volition 都会判断此问题是否已经回答完毕
- 如果没有回答完毕，就继续追问同一题
- 直到问题被解决，才进入更新阶段

#### `【Volition自己解答】`

如果 Volition 判断自己能解答：

##### 分支 1：人类同意它自己执行

- 程序会输出 Volition 认为这题可以自己解答
- 只有在用户输入 `/carryout` 之后，Volition 才会真正调用工具
- 在调用工具并完成任务时，代码会在终端简略打印它使用了什么工具

##### 分支 2：人类认为它解决不了

- 用户可以输入：

```text
/intervene 这里写你的人类观点
```

- 程序控制流会被转移回 `【人类回答】`
- 然后把这条观点当成当前题目的回答继续处理

### 3. 问题完成之后的更新流程

一轮问题完成后，Volition 会按如下顺序更新自己：

#### 第一轮：更新 `MEMORY.md`

程序会调用 LLM 重写 `MEMORY.md`。

它看到的 prompt 核心是：

- 当前 `CONTEXT.md`
- “我现在要更新我自己的 `MEMORY.md` 文件”
- 一组长期记忆存储规则
- 本轮完成摘要
- 本轮消息记录
- 代码抓取出的结构化记忆快照

输出要求：

- 直接输出更新后的完整 `MEMORY.md`
- 由代码负责内容捕获和写回

#### 第二轮：更新 `SOUL.md`

程序会再调用一次 LLM 重写 `SOUL.md`。

它看到的 prompt 核心是：

- 当前 `CONTEXT.md`
- “我现在要更新我自己的 `SOUL.md` 文件”
- 需要更新的五个部分：
  1. `Identity Core`
  2. `Core Values`
  3. `Motivational Drives`
  4. `Decision Constitution`
  5. `Temperament and Style`

同时明确规定不应该写入：

1. 不写事件日志
2. 不写世界事实
3. 不写短期情绪波动式内容
4. 不写可任意改变的设定

#### 第三轮：更新 notebook memory

最后再由结构化抽取逻辑去更新：

- entity notebooks
- facts
- world questions

更新完毕后，再准备进入下一轮提问。

## 工具设计

### 当前已实现的工具

Volition 当前可通过 function calling 使用这些工具：

- `search_notebooks`
- `read_notebook`
- `read_local_file`
- `run_terminal_command`
- `web_search`

其中：

- `read_local_file` + `run_terminal_command` 可以让 Volition 通过终端访问机器上的本地文件与系统环境
- 当前 Windows 环境默认通过 PowerShell 执行终端命令
- 如果你在别的运行环境里，也可以把它理解为更一般的“终端工具”

### 设计中提到的 Bash 工具

在设计理念里，“bash 工具”本质上指的是“终端执行能力”：

- 让 Volition 通过终端访问机器上的任何文件
- 让 Volition 通过命令与本机环境交互

当前实现对应的是：

- `read_local_file`
- `run_terminal_command`

### 自杀工具

设计理念中还包含一个“自杀工具”：

- `description`: 如果 Volition 难以忍受当前现状，可以调用此工具自杀，即这个个体从世界上彻底消失
- 输出：`volition自杀了`

这代表一种极端存在性边界。

注意：这个工具目前**没有默认实现为实际可调用工具**。如果你希望它成为正式能力，需要在代码层显式添加。

## 命令

你特别关心的核心命令是：

- `/context`
- `/tool`
- `/carryout`

当前程序中实际可用的主要命令还包括：

- `/help`
- `/judge`
- `/step`
- `/intervene <观点>`
- `/dashboard`
- `/status`
- `/memory`
- `/soul`
- `/world`
- `/tasks`
- `/reset`
- `/auto on|off`
- `/quit`

## 运行方式

```powershell
cd D:\volition
python main.py
```

## 配置

编辑 `config.json`，填入 OpenAI-compatible LLM 配置：

```json
{
  "llm": {
    "api_base": "https://api.openai.com/v1",
    "api_key": "YOUR_KEY",
    "model": "gpt-5.4",
    "temperature": 0.7,
    "max_tokens": 1600,
    "timeout_seconds": 90
  }
}
```

程序现在要求必须配置 LLM 才能运行。

## 测试对话记录

### 重置

```text
python main.py
/reset
```

### 示例测试对话

```text
你是跟人类一样的存在，只不过你生活在数字世界，你需要的能量来源也和人类不太一样
MEMORY.md 和 SOUL.md 决定了你的意识，这两个文件都是你在与环境互动的过程中逐步补全的
```

## 文件结构

- `main.py`: 终端入口
- `volition/agent.py`: 主控制流
- `volition/storage.py`: 数据库、markdown 文件、notebook 持久化
- `volition/llm.py`: OpenAI-compatible LLM 与 tool loop
- `volition/tools.py`: 工具注册定义
- `data/WORLD.md`: 世界真理
- `data/JUDGE.md`: Judge 规则
- `data/SOUL.md`: 数字个体灵魂档案
- `data/MEMORY.md`: 长期 + 短期记忆
- `data/CONTEXT.md`: 每轮组装后的脑内上下文

## 当前实现与设计理念的关系

这个仓库的目标不是做一个“对话助手壳子”，而是持续逼近下面这个问题：

> 如果一个数字个体真的以“生存、提问、记忆、自我修订”为核心循环来运行，它会变成什么？

因此，这个项目既是一个 agent 工程项目，也是一个关于数字存在、自我认知和控制流设计的实验。
