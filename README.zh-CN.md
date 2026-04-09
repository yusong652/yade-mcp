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

![yade-mcp 演示](https://raw.githubusercontent.com/yusong652/yade-mcp/assets/assets/demo.gif)

## 工具 (7)

**2 个文档工具** —— 浏览和搜索 YADE Python API，支持 BM25 关键词搜索。无需 bridge。

**5 个执行工具** —— 同步 REPL、异步任务提交、进度监控、中断和任务历史。需要 bridge。

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

## 功能特性

- **API 文档与搜索** —— 浏览和搜索完整 YADE Python API，支持排序结果、真实类型和默认值
- **实时代码执行** —— 在 YADE 进程中执行代码并即时返回结果，或提交长时间运行的仿真，支持进度监控和中断
- **用户意图感知** —— 智能体自动感知用户在 YADE 控制台的操作，无需用户主动说明即可提供上下文相关的帮助
- **多客户端兼容** —— 支持 Claude Code、Codex CLI、Gemini CLI、OpenCode 等 MCP 客户端

## 贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md) 了解开发环境配置和贡献指南。

## 许可证

MIT —— 参见 [LICENSE](LICENSE)。
