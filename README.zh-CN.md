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

**yade-mcp** 通过 [Model Context Protocol](https://modelcontextprotocol.io/) 将 AI 智能体连接到 [YADE](https://yade-dem.org/) —— 开源离散元方法引擎。通过自然语言对话即可浏览 API 文档、运行仿真和执行代码。

它不只是调用 YADE 的工具 —— agent 会坐在你的 console 前、自己接管长仿真、和你做的一切保持同步。

![yade-mcp 演示](https://raw.githubusercontent.com/yusong652/yade-mcp/assets/assets/demo.gif)

## 功能特性

### agent 来敲，YADE 来跑

*由 `yade_execute_code` 驱动*

用大白话告诉它你想做什么，agent 就把命令敲进你的 YADE 控制台 —— 查颗粒、调参数、走引擎、看结果。敲完命令看返回，不断调试、迭代，和你自己用 YADE 时一样的感觉。

### 撒手让它跑，agent 自己盯

*由 `yade_execute_task` + `yade_check_task_status` + `yade_interrupt_task` 驱动*

把一份完整的 YADE 脚本作为后台任务跑起来 —— 就像你平时敲 `yade script.py` 一样，但你不用守着。agent 会自己盯：拉实时输出、抓异常、发现不对就优雅停掉、改脚本、再交一次 —— 直到仿真真正跑完。

### 新会话，不冷启动

*由 `yade_list_tasks` + `yade_check_task_status` 驱动*

每一个交过的任务 —— 脚本、实时输出、最终状态 —— 都留了底。哪怕上下文窗口爆了、或者你第二天才回来，新开的 agent 也能直接走进一个"自己记得自己"的项目：列出之前跑过什么、读出每个任务产出了什么 —— 接着干，不用你从头讲。

### 仿真跑着，shell 就在

*由 `yade_execute_code` 驱动*

任务跑的时候，agent 手里就有一把直通仿真的实时 shell —— 查任意变量、抓任意对象的状态、按需画一张新图，全程不动脚本、不停仿真。

### 你来敲，agent 来跟

除了提交过的任务，你在 YADE console 里随手敲的每一行 —— 顺手查的变量、试过的参数、走到一半放弃的方向 —— 都会自动汇入 agent 的上下文。等你转头跟它聊天，它已经掌握了你这一路尝试的痕迹。想让它点评你刚敲的几行？卡在某个意外的报错上？直接问就行 —— 你敲了什么、YADE 怎么回的，agent 都跟得清楚。

## 工具 (7)

两个文档工具（无需 bridge）+ 五个执行工具（需要 bridge）：

| 工具 | 用途 | Bridge |
| --- | --- | --- |
| `yade_browse_api` | 浏览 YADE Python 类树 | 否 |
| `yade_query_api` | 跨 API 文档的 BM25 关键词搜索 | 否 |
| `yade_execute_code` | 在运行中的 YADE 进程中同步执行 Python | 是 |
| `yade_execute_task` | 把脚本作为长时后台任务提交 | 是 |
| `yade_check_task_status` | 查询运行中或已完成任务（输出、状态） | 是 |
| `yade_interrupt_task` | 优雅中断运行中的任务 | 是 |
| `yade_list_tasks` | 列出已提交任务及元数据 | 是 |

## 快速开始

### 前置条件

- 已安装 **[YADE](https://yade-dem.org/doc/installation.html)**
- 已安装 **[uv](https://docs.astral.sh/uv/getting-started/installation/)**（用于 `uvx`）

### 智能体配置（推荐）

将以下内容复制给你的 AI 智能体，让它自动完成配置：

```text
Fetch and follow this bootstrap guide end-to-end:
https://raw.githubusercontent.com/yusong652/yade-mcp/master/docs/agentic/yade-mcp-bootstrap.md
```

### 手动配置

**1. 在客户端配置中注册 MCP 服务器：**

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

**2. 在 YADE 中启动 bridge：**

```bash
pip install yade-mcp-bridge
```

然后在 YADE Python 控制台中：

```python
import yade_mcp_bridge
yade_mcp_bridge.start()
```

### 验证

重启你的 AI 智能体（Claude Code、Codex CLI、Gemini CLI 等），让它调用 `yade_execute_code` 来验证连接。

## 贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md) 了解开发环境配置和贡献指南。

## 许可证

MIT —— 参见 [LICENSE](LICENSE)。
