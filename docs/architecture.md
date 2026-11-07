# 架构与模块边界

本文维护设计、模块边界与跨模块契约；完成情况和任务分配只见 [status.md](status.md)。当前为 Python 核心库 + Typer CLI，入口直接调用函数，无需启动 HTTP 服务。

## 当前流程

```text
M4A → probe / record_input
        ├─ transcribe → ASR transcript.json ─┐
        └─ diarize → speakers.json ─────────┤
                                           ↓
                         annotate → 带说话人转写
                                           ↓
                         refine → 细化归属转写
                                           ↓
                analyze：分块分类 → 选择 Skill → 分块提取 → 全局合并
                                           ↓
                  summary.md / summary.json / sources.md + SQLite 索引
```

ASR 与说话人区分的数据依赖独立，图中的分支不代表已实现自动并行调度。当前需要分别调用音频阶段并传递路径；`analyze_recording` 内部才有自动编排。`refine` 用于改善待确认归属，`analyze` 也接受已完成说话人合并的转写；保留不确定性，不强制把每句话归到某个人。

## 模块与现有调用边界

以下路径相对于 `src/sonafolio/`；函数是当前实际接口，尚未抽象为统一后端协议。

| 模块 | 文件与主要入口 | 输入 → 输出 | 主要验证 |
| --- | --- | --- | --- |
| 工作区与环境 | `paths.py`；根目录 `scripts/workspace.py`、`scripts/bootstrap.py` | 工作区 → 本地目录、受控子进程环境 | `test_workspace.py` |
| 音频与 ASR | `audio.py:record_input/decode_audio`、`models.py`、`transcribe.py:transcribe_local`、`_whisper_worker.py` | 音频／模型 → 元信息、PCM、原始转写 | `test_audio.py` |
| 说话人与对齐 | `diarization.py:diarize_local`、`annotation.py:annotate_recording`、`refinement.py:refine_recording`、`ctc.py` 及相应 worker | 音频／转写／声音区间 → 带归属和不确定性的转写 | `test_diarization.py`、`test_annotation.py`、`test_refinement.py` |
| LLM 适配 | `llm.py:request_json/load_settings` | 指令／文本／Schema → 校验结果和调用元信息 | `test_llm.py` |
| 场景 Skill | `skills.py:SkillRegistry/render_summary`、`builtin_skills/` | 清单／Prompt／Schema／模板 → 场景定义与 Markdown | `test_skills.py` |
| 内容分析与输出 | `analysis.py:analyze_recording` | 带说话人转写 → 分类、提取、汇总、来源页、索引 | `test_skills.py` |
| 用户入口 | `cli.py`、`__main__.py` | 参数 → 核心函数 → JSON 结果或错误退出 | CLI 帮助检查及相关模块测试 |

拆分 Agent 时可按表分工。`cli.py`、依赖与锁文件、公共契约文档属于共享修改点；由集成负责人合并，避免多个分支同时调整入口和协议。未来桌面模块尚无源码目录，不提前创建空目录。

## 阶段产物契约

核心函数大多返回含 `output_directory` 的摘要字典，详细数据写入文件。下一阶段读取对应文件，不把返回摘要当成完整转写。路径统一经过工作区校验，输入原始音频只读。

| 产物 | 当前关键字段与语义 |
| --- | --- |
| `data/runs/<录音与指纹>/input.json` | 原始音频绝对路径 `source`、`sha256`、时长和媒体信息 |
| `asr/<范围与设置>/transcript.json` | `schema_version=1`、`source_sha256`、`range`、`segments`；片段含 `id/start/end/text/speaker_id/role`，ASR 阶段说话人为空 |
| `diarization/<范围与设置>/speakers.json` | 同一音频指纹与范围、`speaker_ids`、允许重叠的 `turns`、单说话人视图 `exclusive_turns`；区间含 `start/end/speaker_id` |
| `annotated/<指纹>/transcript.json` | 保留原文和片段 ID，增加候选声音、覆盖量与归属状态；不清楚时 `speaker_id=null` |
| `refined/<指纹>/transcript.json` | 保留原文，拆分片段通过 `source_segment_id` 关联原始片段；混合精度，字符时间为模型估计 |
| `analysis/<设置指纹>/` | `route-*.json`、`notes-*.json`、带预算和版本的合并结果、状态、预览或失败诊断 |
| `data/output/<类目层级>/<运行标识>/` | `summary.json` 含 `metadata/classification/result`；`summary.md` 的引用指向 `sources.md` 的片段锚点 |
| `data/index.sqlite` | 目前只有 `analyses` 结果索引；不是完整任务队列或统一 Pipeline 状态库 |

时间使用相对于原始录音的秒数，禁止按分块重新归零。声音 ID 仅在同一次录音处理范围内稳定，不表示跨录音身份；业务角色在 LLM 阶段推断。合并前验证来源指纹和范围；声音聚类不能按 LLM 分块重新执行。

转写可为 `completed_with_unresolved_segments`，表示计算完成但仍有归属不确定性。`status.json` 的 `completed` 仅表示阶段执行成功，不证明识别准确。现有产物有基础版本字段，但并非所有字段都有独立 Schema；变更字段、目录层级或状态语义时，要同步所有生产者、消费者、缓存版本及兼容策略。

## 缓存与恢复边界

- ASR、说话人、合并与对齐目录根据输入或参数区分版本，但当前调用这些阶段不会自动跳过已有计算。
- LLM 分类、提取和合并已有文件检查点，重跑会校验并复用；`--force` 重新调用模型。
- 当前分析指纹包含整个转写、全部 Skill 定义及 LLM 设置；只改模板也可能使分析检查点失效。尚未实现独立的“仅重渲染”命令。
- 最终合并预算和 `MERGE_VERSION` 单独区分结果，调整它们可以复用分类和提取检查点。详见 [Skill 协议](skills.md)。
- 统一恢复、任务取消、并发控制和全链路状态机尚未实现，属于下一阶段工作。

## 已采纳的技术方向

当前模型组合为 whisper.cpp / pywhispercpp + Silero VAD、pyannote community-1、中文 Wav2Vec2 CTC，以及 DeepSeek JSON 输出。模型来源和 revision 记录在工作区 `.models/` 的清单内；模型下载与推理显式分离。其他供应商、本地 LLM 和云端 ASR 仍待接入。

后续保留 Python 处理核心，桌面方向采用 **Tauri 2 + TypeScript / React + Python 独立处理进程**。Rust 负责桌面生命周期和系统集成，不重写现有模型管线。CLI 与桌面复用同一核心；暂不为桌面强制引入 HTTP 服务。

以下均为计划，不能当作已存在接口：

1. 新增 `sonafolio run <audio>`，管理阶段依赖、进度、取消、复用与失败恢复；仍保留单阶段命令。
2. 为桌面定义带协议版本、任务 ID 的请求和事件。优先验证通过标准输入／输出传递 JSON Lines，日志与协议分流；具体字段在实现时确定。
3. 桌面通过 sidecar 启动受控 Python 处理进程；分别制作 macOS Apple Silicon / Windows x64 运行包，再评估其他架构。
4. 桌面用户选择可写工作区，环境、模型、推理缓存和结果放入该目录；安装目录与数据目录分离，不向应用包内部写数据。

新编排模块需调用现有核心，不在 CLI、Rust 或前端复制业务规则。正式打包必须验证 Python/native 依赖、GPU 与 CPU 路径、路径迁移、签名和安装体验；Tauri 的平台支持不代表模型依赖已在对应平台验收。

## 设计约束与资料

### AI 原生的整理场景与三类模型任务

用户确认将行业背景、场景类目、Prompt 和输出模板合并为一个“整理场景”。用户通过关键词表达整理意图，AI 起草规则；录音默认自动匹配场景，再执行内容整理。人工主要负责启用规则、处理歧义和核对来源，不要求导入时逐项选分类。

| AI 任务 | 输入 → 输出 | 当前实现边界 |
| --- | --- | --- |
| 场景草稿生成 | 关键词／整理意图 → 场景名称、适用与排除条件、Prompt、输出结构／模板草稿 | 新需求；原型演示，核心接口未实现；草稿不参与路由，编辑确认后启用 |
| 录音场景匹配 | 每条完整转写的分块、已启用场景的路由说明 → 场景 ID、依据与待确认状态 | 已有单录音的 LLM 分类与层级投票；尚未接入新场景条件与批次调度 |
| 内容总结 | 带说话人的转写、所选场景版本 → 分块提取、全局总结、来源引用 | 当前 `analysis.py` 已实现单录音提取与合并；保留现有证据校验 |

三者是任务目的，不是固定三次 API 请求；共用凭据与供应商适配入口，分别记录输入版本、模型、用量、错误和重试。缓存按任务目的隔离，避免“改规则”与“执行规则”互相污染。后续可按任务目的选择模型，但第一版可以共用一个默认模型配置。

产品中的“整理场景”映射为现有声明式 Skill 包：稳定 ID / 版本 / 启用状态、Prompt、JSON Schema 与 Markdown 模板。行业语境进入场景说明与 Prompt，不新增独立行业分类表。关键词用于作者表达意图；触发／排除条件进入路由说明，不能用字符串命中代替语义判断。现有 Skill v1 只读取 `description` 路由，新字段和草稿持久化需先明确协议与兼容策略，不能只在 UI 中增加字段后声称核心已生效。

AI 生成的 Schema 与模板须通过结构、继承、路径、沙箱渲染及示例校验；只生成声明式资源，不执行生成的代码。匹配只能返回本次提供的可用场景 ID；有歧义或无匹配时保留转写和候选依据，不自动创建场景或默默套用任意模板。

批处理按录音指纹独立建任务；ASR / 说话人完成后再据内容匹配，不从文件名推断。每条任务冻结场景目录和选中版本快照，编辑仅影响新任务或显式重跑。失败、取消与待确认局限于当前任务，其他录音继续；人工确认后从内容整理阶段续跑。生产实现需分别限制本地模型与 LLM 请求并发，原型的两条并发只是交互演示。

### 桌面评审原型（v2，待评审）

[desktop-prototype.html](desktop-prototype.html) 是单文件离线交互稿，可直接在浏览器打开。v2 保留录音库、复盘与引用核对、模型 / API Key、工作区 / 模型缓存和重命名；将原有行业／类目／Prompt 管理合为整理场景，新增“关键词 → AI 草稿 → 编辑启用”及“批量导入 → 自动匹配 → 歧义确认 → 继续整理”的可点击流程。布局和交互细节仍待用户评审。

使用 `html-prototype` 与 `design-artifact` 技能制作；基于 MacWhisper、Granola、Notta 的官方公开资料提炼操作方式，引用和设计理由嵌在原型的“设计说明”中。未登录这些产品做完整实操验收。

所有生成、匹配、总结与进度均为标明的预设演示；配置仅存内存，刷新清空，不调用 API 或模型，不改本地文件。任意本地文件只读取名称，无转写内容时保留未匹配，不假装已理解录音。场景生成、批次调度、目录切换、场景版本管理和原文件重命名尚未接入核心；正式实现需补持久化、凭据边界、路径迁移、冲突处理及缓存失效策略。不能直接把原型视为生产前端。

### 核心约束

原始声音区间、转写和来源引用均保留；不可靠归属不强行指派；不将 ASR 误识当作候选人能力缺陷；Schema 与编号合法不等于事实准确。工作区边界见 [workspace.md](workspace.md)，产品验收见 [requirements.md](requirements.md)。

官方参考：[whisper.cpp](https://github.com/ggml-org/whisper.cpp)、[pyannote community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)、[中文对齐模型](https://huggingface.co/jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn)、[DeepSeek](https://api-docs.deepseek.com/)、[Tauri sidecar](https://v2.tauri.app/develop/sidecar/)。实际依赖版本以 `uv.lock` 为准，外部兼容性以目标平台实测为准。
