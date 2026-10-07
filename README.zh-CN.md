# yade-mcp

<p align="center">
  <img src="https://raw.githubusercontent.com/yusong652/yade-mcp/assets/assets/header.gif" alt="yade-mcp header" width="720">
</p>

[English](https://github.com/yusong652/yade-mcp/blob/master/README.md) | [简体中文](https://github.com/yusong652/yade-mcp/blob/master/README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/yade-mcp)](https://pypi.org/project/yade-mcp/)
[![Downloads](https://static.pepy.tech/badge/yade-mcp)](https://pepy.tech/project/yade-mcp)
[![GitHub stars](https://img.shields.io/github/stars/yusong652/yade-mcp)](https://github.com/yusong652/yade-mcp/stargazers)
[![Glama](https://glama.ai/mcp/servers/yusong652/yade-mcp/badges/score.svg)](https://glama.ai/mcp/servers/yusong652/yade-mcp)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)

`O.engines += [LLM()]  # yet another engine.`

**yade-mcp** 通过 [Model Context Protocol](https://modelcontextprotocol.io/) 把 AI 智能体接到开源离散元引擎 [YADE](https://yade-dem.org/) 上。智能体可以浏览 YADE API、在运行中的 YADE 会话里执行代码、把长仿真作为后台任务跑起来，还能读到你在 YADE 控制台敲了什么。

![yade-mcp 演示](https://raw.githubusercontent.com/yusong652/yade-mcp/assets/assets/demo.gif)

## 工具 (7)

两个文档工具（无需 bridge）加五个执行工具（需要 bridge）：

| 工具 | 用途 | Bridge |
| --- | --- | --- |
| `yade_browse_api` | 浏览 YADE Python 类树 | 否 |
| `yade_query_api` | 跨 API 文档的 BM25 关键词搜索 | 否 |
| `yade_execute_code` | 在运行中的 YADE 进程里同步执行 Python | 是 |
| `yade_execute_task` | 把脚本作为长时后台任务提交 | 是 |
| `yade_check_task_status` | 查看运行中或已结束任务的输出和状态 | 是 |
| `yade_interrupt_task` | 在迭代边界停下运行中的任务，或撤销排队中的任务 | 是 |
| `yade_list_tasks` | 列出已提交任务及其元数据 | 是 |

## 提示词示例

- *"在一个密实试样上做三轴压缩试验，画出偏应力随轴向应变的曲线"*
- *"用 level set 建一个不规则颗粒，让它落到平面上"*
- *"仿真还在跑，别停，看一下现在的不平衡力"*
- *"查一下 GlobalStiffnessTimeStepper 是怎么选时间步的，然后把它加到这个模型里"*
- *"我刚在控制台敲的那条命令报错了，哪里不对？"*
- *"列一下昨天跑过什么，每个任务的结果各总结一下"*

## 首次配置

### 前置条件

- 已安装 **[YADE](https://yade-dem.org/doc/installation.html)**
- 已安装 **[uv](https://docs.astral.sh/uv/getting-started/installation/)**。智能体客户端靠它提供的 `uvx` 启动 `yade-mcp`，没装的话客户端启动服务器时会报 `No such file or directory`。
- **一个 AI 智能体**：Claude Code、Codex CLI、Gemini CLI，或任意支持 MCP 的客户端

### 智能体配置（推荐）

把下面这段复制给你的 AI 智能体，让它自己完成配置：

```text
Fetch and follow this bootstrap guide end-to-end:
https://raw.githubusercontent.com/yusong652/yade-mcp/master/docs/agentic/yade-mcp-bootstrap.md
```

### 手动配置

**1. 注册 MCP 服务器**（按你的客户端选一行）：

```bash
# Claude Code
claude mcp add yade-mcp -- uvx yade-mcp

# Codex CLI
codex mcp add yade-mcp -- uvx yade-mcp

# Gemini CLI
gemini mcp add yade-mcp uvx yade-mcp
```

或者手动填 MCP 配置文件：

```json
{
  "mcpServers": {
    "yade-mcp": {
      "command": "uvx",
      "args": ["yade-mcp"]
    }
  }
}
```

**2. 把 bridge 装进 YADE 自己的 Python。**

YADE 内嵌的是一个固定的解释器（Debian / Ubuntu 软件包用的是系统 `python3`，源码编译用的是编译时链接的那个）。conda 或 venv 里的 Python 是另一个解释器，装在那里的包 YADE 看不到。最稳的办法是从 YADE 内部安装，那里的 `sys.executable` 就是正确的解释器。在 YADE 控制台里：

```python
import sys, subprocess
subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "yade-mcp-bridge"])
```

两种可能的报错：

- `No module named pip`：这个解释器没有 pip。用系统包管理器装上，例如 `sudo apt install python3-pip`。
- `externally-managed-environment`（Debian 12、Ubuntu 23.04 及之后）：pip 默认拒绝 `--user`。在上面命令的 `"--user"` 后面加一项 `"--break-system-packages"`。

装完退出并重启 YADE，新的包目录才会被加载。

bridge 已提交合入 YADE 本体（[yade-dev/trunk !1187](https://gitlab.com/yade-dev/trunk/-/merge_requests/1187)）。随 YADE 发布之后这一步就不需要了，直接 `from yade import mcpbridge` 启动。

**3. 启动 bridge**，在 YADE 控制台里：

```python
import yade_mcp_bridge
yade_mcp_bridge.start()
```

会打印一行：`YADE MCP Bridge on http://localhost:9002, log: <cwd>/.yade-mcp/bridge.log`。

### 验证

重启你的 AI 智能体，让它确认已连上 YADE。它会调用 `yade_execute_code`，返回里的 `ok: true` 表示整条链路通了。

## 日常启动

首次配置完成后，每次新开 YADE 会话只需要把 bridge 再启动一次。在 YADE 控制台里：

```python
import yade_mcp_bridge
yade_mcp_bridge.start()
```

MCP 客户端的配置是持久的，不用重做。

**端口与容器。** `start()` 接受 `port`（默认 9002）和 `host` 参数。改了端口就必须告诉 MCP 服务器，否则它还会连 9002。用对应的 URL 重新注册：

```bash
codex mcp remove yade-mcp
codex mcp add yade-mcp -- uvx yade-mcp --bridge-url http://localhost:9008
```

在容器里运行时用 `yade_mcp_bridge.start(host="0.0.0.0")` 启动，bridge 才能从容器外访问。

## 功能特性

- **运行中 YADE 进程里的实时 REPL**：`yade_execute_code` 在会话自己的命名空间里执行 Python，状态在调用之间保持。任务运行期间照常可用，不停仿真也能读中间结果。
- **任务生命周期**：把脚本作为后台任务提交、跟踪输出、在迭代边界停下、改完再交。任务排队依次执行，多阶段流水线可以一次性全部提交。
- **跨会话的任务历史**：每个任务的脚本、输出和最终状态都留底。新开的智能体会话列出之前跑过什么，不用你重新交代。
- **控制台输入同步给智能体**：你在 YADE 控制台敲的每一行，智能体下次调用时都能看到，可以一边用控制台一边和智能体协作。
- **无需 bridge 的 API 文档**：类树浏览和 YADE Python API 的 BM25 搜索离线可用，语料按当前 YADE 版本刷新。
- **多客户端**：支持 Claude Code、Codex CLI、Gemini CLI、GitHub Copilot CLI、OpenCode、toyoura-nagisa 及其他 MCP 客户端。

## 排障

见 bootstrap 指南中的 [Troubleshooting](docs/agentic/yade-mcp-bootstrap.md#troubleshooting)。

## 贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md) 了解开发环境配置和贡献指南。

## 许可证

MIT，参见 [LICENSE](LICENSE)。
