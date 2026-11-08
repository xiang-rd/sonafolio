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

ASR 与说话人区分的数据依赖独立，图中的分支不代表已实现自动并行调度。`pipeline.py:run_pipeline` 现已顺序编排这些阶段，也可继续分别调用阶段函数；目前不自动并行执行 ASR 与说话人区分。`refine` 用于改善待确认归属，`analyze` 也接受已完成说话人合并的转写；保留不确定性，不强制把每句话归到某个人。

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
| 任务存储与编排 | `tasks.py`、`pipeline.py`、`processes.py`、`_pipeline_worker.py`、`migration.py` | 录音 → 受管任务、阶段版本、进度事件、恢复／取消、复制迁移与索引重建 | `test_tasks_pipeline.py` |
| 用户入口 | `cli.py`、`__main__.py` | 参数 → 核心函数 → JSON 结果或错误退出 | CLI 帮助检查及相关模块测试 |

拆分 Agent 时可按表分工。`cli.py`、依赖与锁文件、公共契约文档属于共享修改点；由集成负责人合并，避免多个分支同时调整入口和协议。桌面代码位于 `desktop/`，Rust 宿主只负责进程与系统集成，React 不复制核心业务规则。

## 阶段产物契约

核心函数大多返回含 `output_directory` 的摘要字典，详细数据写入文件。下一阶段读取对应文件，不把返回摘要当成完整转写。路径统一经过工作区校验，输入原始音频只读。

| 产物 | 当前关键字段与语义 |
| --- | --- |
| `data/runs/<完整 SHA-256>/manifest.json` | `schema_version=1`、稳定 `task_id`、标题、原音频信息与相对路径、任务状态、配置、各阶段尝试和分析版本；旧 `input.json` 仍可读 |
| `asr/<范围与设置>/transcript.json` | `schema_version=1`、`source_sha256`、`range`、`segments`；片段含 `id/start/end/text/speaker_id/role`，ASR 阶段说话人为空 |
| `diarization/<范围与设置>/speakers.json` | 同一音频指纹与范围、`speaker_ids`、允许重叠的 `turns`、单说话人视图 `exclusive_turns`；区间含 `start/end/speaker_id` |
| `alignment/<归属版本>/transcript.json` | 保留原文和片段 ID，增加候选声音、覆盖量与归属状态；不清楚时 `speaker_id=null` |
| `alignment/<细化版本>/transcript.json` | 保留原文，拆分片段通过 `source_segment_id` 关联原始片段；混合精度，字符时间为模型估计 |
| `analysis/<分析版本>/checkpoints/` | `route-*.json`、`notes-*.json`、带预算和版本的合并结果、状态、预览或失败诊断 |
| `analysis/<分析版本>/` | `summary.json` 含 `metadata/classification/result`；`summary.md` 的引用指向 `sources.md` 的片段锚点 |
| `data/index.sqlite` | 目前只有 `analyses` 结果索引；不是完整任务队列或统一 Pipeline 状态库 |

时间使用相对于原始录音的秒数，禁止按分块重新归零。声音 ID 仅在同一次录音处理范围内稳定，不表示跨录音身份；业务角色在 LLM 阶段推断。合并前验证来源指纹和范围；声音聚类不能按 LLM 分块重新执行。

转写可为 `completed_with_unresolved_segments`，表示计算完成但仍有归属不确定性。`status.json` 的 `completed` 仅表示阶段执行成功，不证明识别准确。现有产物有基础版本字段，但并非所有字段都有独立 Schema；变更字段、目录层级或状态语义时，要同步所有生产者、消费者、缓存版本及兼容策略。

## 任务数据布局

2026-11-08 确认并实现：新录音导入时将原始音频复制到任务内，取消顶层 `input/`、`output/` 的新写入。run 是长期稳定的录音任务，不是每次命令调用；任务 ID 为完整音频 SHA-256，相同内容再次导入返回同一任务。旧目录继续保留、可读，通过独立命令复制迁移。

```text
data/
  runs/
    <任务 ID>/
      input/                 原始录音
      manifest.json          任务身份、文件信息、状态、配置与版本
      asr/                   转写及阶段版本
      diarization/           说话人区间及阶段版本
      alignment/             合并、对齐与归属结果及阶段版本
      analysis/
        <分析版本>/
          catalog.json       分析开始时的场景目录快照
          scene.json         本次使用的场景快照
          checkpoints/       分类、分块提取、合并与恢复记录
          summary.json
          summary.md
          sources.md
  index.sqlite               跨任务查询与列表索引
```

- 任务 ID 独立于显示标题和原文件名；录音指纹用于完整性与复用校验，重命名不改变任务身份或引用。
- 每个任务目录包含其处理所需的录音、阶段产物和分析版本，不依赖另一个任务目录；模型、下载缓存、密钥和全局场景定义仍独立管理。分析所用场景保存快照，不以最新场景覆盖历史。
- 修改 Prompt、场景或模型后，按依赖与参数生成新的阶段／分析版本；可复用的转写与说话人结果保留，历史结果不隐式覆盖。manifest 的 `stages` 按阶段保存尝试列表，每项包含签名、状态、相对目录、输入／设置指纹和完成产物的 SHA-256。签名不匹配或产物丢失／损坏时不复用；`--force` 新建尝试，保留历史。
- 后续批量导入将创建多个任务，通过批次 ID 关联；当前仅预留 manifest 的 `batch_ids`，批次调度尚未实现。批次不拥有唯一的原始录音副本。按场景组织主要通过索引和筛选实现，不再把唯一结果分散到顶层类目目录。
- SQLite 保存跨任务索引，任务文件是索引重建的依据；完整任务目录应可独立备份和导出。`rebuild-index` 校验受管音频、总结、转写与来源引用后，以事务重建受管分析行，保留旧版索引行；SQLite 仍只有 `analyses` 表，任务状态以 manifest 为准。
- `migrate-run` 默认预览，`--apply` 复制旧 `annotated/`、`refined/` 到 `alignment/`，汇集旧总结与检查点，更新 JSON 路径并校验来源链接和索引。失败撤回本次发布的派生目录，原录音与旧结果始终保留；可能已导入的受管录音也保留。旧数据没有历史 Skill 全文时，`scene.json` 明确记录 `legacy_snapshot_unavailable`，不拿当前规则冒充历史。迁移产物用于查看和留档，尚不自动转换为新 Pipeline 的可复用尝试。
- `data/validation/` 现有私有验证记录不是生产任务目录的一部分，本方案不移动或删除这些记录。

## 缓存与恢复边界

- 通过 `run` 执行时，按阶段输入内容、模型文件／revision 清单、后端版本与设置判断复用，并核对产物哈希；单阶段命令仍不自动跳过音频推理。模型目录的 revision 清单应与权重一起管理，不能修改权重后保留旧清单。
- LLM 分类、提取和合并已有文件检查点，重跑会校验并复用；`--force` 重新调用模型。
- 当前分析指纹包含整个转写、全部 Skill 定义及 LLM 设置；只改模板也可能使分析检查点失效。尚未实现独立的“仅重渲染”命令。
- 最终合并预算和 `MERGE_VERSION` 单独区分结果，调整它们可以复用分类和提取检查点。详见 [Skill 协议](skills.md)。
- 统一任务状态为 `imported/running/completed/preview_only/failed/cancelled`，阶段尝试为 `running/completed/failed/cancelled`。同任务通过 OS 文件锁拒绝并行写入；锁随进程退出释放，残留锁文件不代表仍在运行。硬中断可能留下 running 尝试，下次可重算，分析会续用已校验的分块检查点。
- Pipeline 每阶段在独立 Python 进程中执行，取消令牌或 Ctrl+C 会清理推理进程组；Windows 使用进程树终止，尚待实机验收。不同任务的全局模型并发限制及批次恢复属于 T09。

`run_pipeline(audio, options=RunOptions(...), cancelled=..., on_event=...)` 是核心入口；`resume_pipeline(task_id)` 沿用保存的参数。事件包含 `task_id/stage/state`，CLI 将事件写 stderr、最终 JSON 写 stdout；当前是阶段粒度，原生模型详细进度仍在各阶段本地文件。桌面使用下述版本化 IPC 封装这些事件。

默认 `analysis=preview`，完成本地识别／说话人与可选细化后生成发送计划；`none` 止于转写，`cloud` 显式发送转写给 DeepSeek。失败的分析继续使用自己的未完成版本，成功历史保持独立；只改合并预算时可复制匹配签名的分类／提取检查点，新预算仍生成独立结果。

## 已采纳的技术方向

当前模型组合为 whisper.cpp / pywhispercpp + Silero VAD、pyannote community-1、中文 Wav2Vec2 CTC，以及 DeepSeek JSON 输出。模型来源和 revision 记录在工作区 `.models/` 的清单内；模型下载与推理显式分离。其他供应商、本地 LLM 和云端 ASR 仍待接入。

当前保留 Python 处理核心，桌面采用 **Tauri 2 + TypeScript / React + Python 独立处理进程**。Rust 负责桌面生命周期和系统集成，不重写现有模型管线。CLI 与桌面复用同一核心；暂不为桌面强制引入 HTTP 服务。

桌面集成与当前实现：

1. 已有 `sonafolio run <audio>`；后续桌面复用核心编排，仍保留单阶段命令。
2. 已有 `sonafolio ipc` 标准输入／输出 JSON Lines 桥接，协议版本为 1；Rust 宿主与 React 消费端已接入。
3. `desktop/src-tauri/src/main.rs` 从 `.app/Contents/Resources/runtime` 启动内置 Python。`scripts/build_macos_runtime.py` 由受管 CPython 和 `uv.lock` 全部 extras 构建独立运行包，模型权重另存工作区；当前为 macOS arm64 开发版，Windows 实机暂缓。
4. 用户显式选择可写工作区，模型、推理缓存、配置、密钥、结果及日志写入该目录；Python 和依赖随应用只读分发，不在用户机器全局安装。WebView 使用 incognito，外观配置存工作区 TOML，启动不读取其他浏览器配置。

新编排模块需调用现有核心，不在 CLI、Rust 或前端复制业务规则。正式打包必须验证 Python/native 依赖、GPU 与 CPU 路径、路径迁移、签名和安装体验；Tauri 的平台支持不代表模型依赖已在对应平台验收。

## 本地桌面协议 v1

`ipc.py:DesktopServer` 复用 Pipeline 和任务存储；`sonafolio ipc` 作为受信任本地宿主的子进程运行，通过 UTF-8 JSON Lines 通信，不监听网络端口。所有输出帧包含 `protocol_version=1`、单调递增的 `seq` 和 `type`。输入／输出及各方法参数 Schema 可通过 `sonafolio ipc --schema` 导出；参数拒绝未知字段及隐式字符串／布尔转换。`RunOptions` 的默认值和类型由核心派生。

请求示例：

```json
{"protocol_version":1,"id":"req-1","method":"hello","params":{}}
```

成功响应为 `type=response,id,ok=true,result`；错误响应为 `type=response,id,ok=false,error={code,message}`。无法解析请求时 `id=null`。事件为 `type=event,event,data`，任务事件另含 `operation_id`。首次输出 `server.ready`，随后通过 `task.progress` 传递阶段状态，以 `operation.finished` 结束一次操作；异步请求的接受响应先于该操作的任何事件。

| 方法 | 参数与结果 |
| --- | --- |
| `hello` | 返回版本、平台、方法、限额和默认分析模式 |
| `tasks.list` | `offset/limit`；返回任务摘要、总数和不可读记录数，不读取全部转写；损坏记录不阻断正常列表 |
| `tasks.get` | `task_id`；返回标题、状态、阶段摘要、音频基本信息、分析版本及最新结果相对目录 |
| `tasks.run` | `audio/options/force`；默认 preview，显式 cloud 才请求 LLM；返回排队状态和 `operation_id` |
| `tasks.resume` | `task_id`，可选 `analysis`；省略时沿用任务保存的分析模式 |
| `operations.get` | `operation_id`；查询本次服务会话中的状态、结果或稳定错误 |
| `operations.cancel` | `operation_id`；设置取消令牌，等待 `operation.finished` 确认终态；取消请求可能与正常完成交错 |
| `tasks.rename` | `task_id/title`；更新显示标题，沿用核心任务锁 |
| `settings.get/save` | 查询工作区配置；保存 LLM、写入式 Key、数据目录和外观，处理期间拒绝修改；返回是否已配置，不返回密钥 |
| `scenes.list` | 返回已启用场景的解析后 Prompt、Schema 与模板；当前只读 |
| `artifacts.read` | `task_id/path/offset/limit`；仅允许任务内 JSON / Markdown，相对路径不得越界或指向原音频；返回 base64 字节块、下一偏移和 EOF |

异步操作状态为 `queued/running/cancelling/completed/preview_only/failed/cancelled`，`operation_id` 区分同任务的不同调用，稳定 `task_id` 仍是录音指纹。服务一次只接受一个处理操作，忙时返回 `BUSY`；控制请求继续响应。保留最近 128 个操作和 2048 个请求 ID；保留窗口内重复请求 ID 返回 `DUPLICATE_REQUEST`，不再次执行。跨会话任务状态仍通过 manifest 和 `tasks.get` 恢复；批次调度属于 T09。

输入一行最多 1 MiB，超长行被完整丢弃后可继续处理后续请求。`artifacts.read` 每块最多 128 KiB，按字节偏移；客户端应拼接字节或使用流式 UTF-8 解码，避免中文跨块截断。文件内容不自动出现在任务列表和进度中。越界、格式或参数错误不回显请求原文；处理失败仅暴露稳定错误码与异常类型，具体诊断留在本地阶段日志。

宿主必须持续读取 stdout 并保持 stdin 开放；EOF、SIGINT 或 SIGTERM 会请求取消并等待当前处理退出，沿用 Pipeline 的子进程树清理。Python 打印重定向至 stderr，原生推理日志留在阶段文件，stdout 保持协议帧。同步导入、指纹计算等本地 I/O 在返回后观察取消令牌。此接口仅供本地受信任宿主，未提供网络鉴权或远程服务。

本轮以模拟宿主验证 Schema、消息关联／顺序、异常恢复、忙碌／取消和字节分块，并在 macOS 真实子进程验证 EOF／SIGTERM 退出与日志分流；本轮还以真实 Tauri / React 窗口验证了工作区连接、总结／转写读取、重命名与配置保存；独立运行包另有原生库、模型短样本与搬迁验证。完整跨平台和公开发行验收仍待完成。

## macOS 运行包与真实界面

`desktop/src/bridge.ts` 将 Tauri command 封装为协议调用，先订阅事件再连接；Rust 读线程关联请求／响应并转发事件。每次工作区连接带独立 connection ID，旧连接的退出事件不会覆盖新会话；React 缓冲早于接受 Promise 返回的进度／终态，避免快速任务产生悬挂状态。Rust 宿主关闭 stdin、等待退出，必要时发送 SIGTERM；工作区 `.runtime/logs/desktop-backend.log` 保存后端诊断，界面不显示凭据或异常正文。

React 当前接入录音列表／状态筛选、原生文件选择、会话内串行多文件导入、任务取消与恢复、显示标题修改、总结／转写／发送预览、引用核对、Finder 定位、只读场景目录、DeepSeek 配置与工作区数据目录。后端配置由 `desktop_settings.py` 保留其他 TOML 表，Key 单独保存为 0600 文件；文件存在检测用于展示本地模型准备状态。目录修改不自动迁移数据，固定模型／缓存目录尚未提供编辑；AI 场景作者、持久批次、角色校正和音频播放属于后续工作。

运行包包含受管 CPython 3.12、锁定的全部处理依赖、应用 wheel、启动器和版本／锁文件清单。包内 TorchCodec 固定使用 PyAV 已携带的 FFmpeg 8 ABI，改写 Mach-O 依赖后本地重签名，避免从 Homebrew 借库。运行验证通过 dyld 已加载库检查和合成音频解码，而非仅凭 import 判定。构建不打包模型权重、数据目录、密钥或开发虚拟环境；源码 sdist 包含 desktop 源码与两个前端／Rust 锁文件，排除 node_modules、产物和生成 Schema。

本机已验证中文与空格路径搬迁、受限 PATH 下独立启动、包内真实 ASR／pyannote／一个片段的 CTC 推理及本地 IPC。当前 .app 为开发构建，目标 macOS 14+ / arm64；只在当前开发机实测，不代表所有 macOS 14 设备或 Intel Mac 已验收。Developer ID 签名、公证、公开分发许可检查、长录音包内回归、Metal 加速与冷启动安装体验仍需后续验收。

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

### 桌面交互原型（布局已确认）

[desktop-prototype.html](desktop-prototype.html) 是单文件离线交互稿，可直接在浏览器打开。v2 保留录音库、复盘与引用核对、模型 / API Key、工作区 / 录音与结果 / 模型 / 缓存目录和重命名；将原有行业／类目／Prompt 管理合为整理场景，新增“关键词 → AI 草稿 → 编辑启用”及“批量导入 → 自动匹配 → 歧义确认 → 继续整理”的可点击流程。2026-11-08 用户确认当前布局，后续按此实现；真实实现位于 `desktop/`，保留此文件作设计参考。

使用 `html-prototype` 与 `design-artifact` 技能制作；基于 MacWhisper、Granola、Notta 的官方公开资料提炼操作方式，引用和设计理由嵌在原型的“设计说明”中。未登录这些产品做完整实操验收。

所有生成、匹配、总结与进度均为标明的预设演示；配置仅存内存，刷新清空，不调用 API 或模型，不改本地文件。任意本地文件只读取名称，无转写内容时保留未匹配，不假装已理解录音。场景生成、批次调度、目录切换、场景版本管理和原文件重命名尚未接入核心；正式实现需补持久化、凭据边界、路径迁移、冲突处理及缓存失效策略。不能直接把此 HTML 当作真实桌面前端，实际接入范围见上方 macOS 章节。

### 核心约束

原始声音区间、转写和来源引用均保留；不可靠归属不强行指派；不将 ASR 误识当作候选人能力缺陷；Schema 与编号合法不等于事实准确。工作区边界见 [workspace.md](workspace.md)，产品验收见 [requirements.md](requirements.md)。

官方参考：[whisper.cpp](https://github.com/ggml-org/whisper.cpp)、[pyannote community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)、[中文对齐模型](https://huggingface.co/jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn)、[DeepSeek](https://api-docs.deepseek.com/)、[Tauri sidecar](https://v2.tauri.app/develop/sidecar/)。实际依赖版本以 `uv.lock` 为准，外部兼容性以目标平台实测为准。
