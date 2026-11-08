# 声笺 · Sonafolio

**让声音，落在纸上。**

将中文录音转写为带说话人、时间戳和来源引用的记录，再通过可配置 Skill 分类、提取与总结。面试场景以候选人视角复盘；类目还包括问诊、快递，并支持自定义和子类目。

AI 原生方向：批量录音由 AI 自动匹配“整理场景”，新场景由用户关键词生成可编辑规则。相关交互见 v2 设计稿，场景生成接口和批次编排尚待接入核心。

目前是 **0.1.0 原型**：已在 Apple Silicon macOS 上分阶段跑通一条约 73 分钟的真实面试录音。各阶段可独立调用，`analyze` 已串联分类、提取和汇总；统一的 `sonafolio run`、Windows 实机验收和桌面界面尚未实现。准确率尚未经过系统人工评估。

## 文档入口

| 文档 | 负责回答 |
| --- | --- |
| [当前状态与下一步](docs/status.md) | 做到了哪里、有什么证据、下一项任务是什么 |
| [需求](docs/requirements.md) | 产品目标、用户约束与验收要求 |
| [架构与模块边界](docs/architecture.md) | 当前调用关系、数据契约、未来桌面方案和拆分边界 |
| [桌面交互设计稿](docs/desktop-prototype.html) | 布局已确认的功能原型：统一整理场景、关键词起草、批量自动匹配、总结与设置 |
| [开发与运行](docs/workspace.md) | 安装、命令、密钥位置、环境隔离和验证方法 |
| [场景 Skill 协议](docs/skills.md) | 如何新增类目、Prompt、输出结构和模板 |
| [Agent 协作入口](AGENTS.md) | 接手顺序、并行协作与交付要求 |

进度只维护在 `docs/status.md`，不再另建周报、任务清单或交接日志。

## 快速开始

先准备 uv 和用于启动安装脚本的 Python 3.10+。脚本将 uv 复制到工作区，并安装项目专用 Python 与依赖；不会向现有 Python 安装包。以下命令在项目根目录执行，Windows 将 `python3` 替换为 `py -3`。

```sh
python3 -B scripts/bootstrap.py --asr --diarization --alignment
python3 -B scripts/workspace.py run -- sonafolio --help
python3 -B scripts/workspace.py run -- sonafolio probe "data/input/recording.m4a"
```

依赖安装不自动下载模型或发送录音。模型准备、各阶段调用和 DeepSeek 配置见[开发与运行](docs/workspace.md)。目前 ASR、说话人区分和对齐在本地执行，LLM 仅实现 DeepSeek 接口；其他本地／云端后端属于后续扩展。

## 文件布局

```text
sonafolio/
  AGENTS.md            Agent 协作规则
  docs/                需求、架构、状态、运行、Skill
  scripts/             环境初始化与启动器
  src/sonafolio/       核心、CLI、独立推理进程和内置 Skill
  tests/               自动化测试
  sonafolio.example.toml  配置示例
  pyproject.toml       依赖与发布配置
  uv.lock              锁定依赖
  .runtime/            项目 Python、uv、密钥与运行状态
  .venv/               项目虚拟环境
  .models/             本地模型
  .cache/              下载与推理框架缓存
  .tmp/                临时文件
  data/                输入、阶段产物、类目结果、验证记录与索引
  dist/                构建产物
```

以上为当前布局。已确认的后续数据方案按录音任务聚合：`data/runs/<任务 ID>/` 包含原始录音、阶段结果和分析版本，全局 SQLite 保留在 `data/`；尚未迁移，详见[数据布局设计](docs/architecture.md#已确认的数据布局待实施)。

真实录音、密钥、模型和环境均已忽略版本管理，不进入源码发布包。当前使用本地 Git，远程仓库尚未配置。代码许可证待选择；模型权重遵循各自许可证，不随源码分发。
