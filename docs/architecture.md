# 技术方案草案

## 建议架构

Python 核心库 + Typer CLI；CLI 直接调用核心，无需先启动 HTTP 服务。任务量采用单进程顺序处理、SQLite 状态与阶段产物即可。后续 FastAPI 和界面复用同一核心。

```text
输入 M4A（只读）
  → 探测声道、采样率和时长
  → 必要时解码为工作区内的派生音频
  → ASR 时间戳转写 + 独立说话人分段
  → 时间轴对齐、说话人归属、重叠与未知状态
  → 配置类目中的主类目、子类目和标签
  → Skill：LLM 根据各人内容推断角色并结构化提取
  → 候选人视角总结（角色不确定时保留未知）
  → JSON、Markdown、按类目归档和 SQLite 索引
```

若输入确实是按参与者分离的双声道，可利用声道信息；普通立体声不能直接当作两个人。保留原始声道，按各后端要求生成派生音频。

## 模型路线

| 路线 | 组合 | 用途与限制 |
| --- | --- | --- |
| 本地基线，建议先验证 | whisper.cpp ASR + pyannote community-1 说话人区分 + 可配置 LLM | whisper.cpp 支持 Mac/Windows，Apple Silicon 可用 Metal。pyannote 独立提供说话人区间；两者须实现时间轴对齐，不能只按一句话最大重叠时间草率处理跨人发言。 |
| 集成备选 | WhisperX + pyannote + 可配置 LLM | 可作为转写、对齐和说话人整合的参考路线；需验证中文对齐模型可用性、依赖和本机耗时，再决定是否整体引入。 |
| 云端／混合 | 提供说话人时间戳的云端 ASR，或本地 ASR + 云端说话人服务；LLM 独立配置 | 接入前验证中文长音频、文件限制、异步任务、说话人数和时间戳字段；不可假设所有 ASR API 都支持说话人区分。 |

faster-whisper 可以作为额外 Python ASR 后端，支持 CPU/NVIDIA GPU 路线；不把它的 GPU 支持等同于 Apple Metal 支持。

pyannote community-1 初次下载需要用户接受 Hugging Face 上的访问条件并提供令牌。其模型采用 CC-BY-4.0，代码与模型许可证分别记录。模型下载完成后支持离线运行，运行时显式引用本地路径。禁用可选遥测。下载条件由用户处理，项目不代替用户接受。

当前本地 ASR 适配器使用 pywhispercpp 绑定 whisper.cpp，已在 Apple Silicon 上加载公开的 large-v3-turbo Q5 模型并使用 Metal。加入显式下载的 Silero VAD 跳过非语音片段；下载记录模型仓库 revision 并校验 SHA-256。短样本已运行，仍有中文专有名词误识别，尚未评估人工准确率；Windows 和 macOS 分别验收。pyannote 的实际计算设备支持也单独验证，不沿用 ASR 的加速结论。

## 说话人与长录音

- 保存全局说话人区间，ASR 分块必须恢复原始全局时间；说话人聚类保持整条录音的一致性。
- 对齐优先使用细粒度时间戳，跨说话人边界的转写需要拆分或补充对齐；无法可靠拆分则标记归属待确认。
- 保存原始重叠结果；供易读转写使用的单说话人视图不能覆盖重叠事实。
- 不把第一个说话人、说话最多的人或最后回答问题的人固定当作候选人。
- 角色映射独立存储并支持人工覆盖，不重新做 ASR 或聚类。
- 不要求用户预先标注身份，也不预设只有两人；底层只输出稳定说话人 ID，业务角色由总结阶段的 LLM 根据内容推断。
- 摘要按段提取证据再全局汇总；避免把完整长文本截断后交给模型。
- 每一步独立记录状态、错误、输入指纹、后端及参数；重跑只使受影响的下游产物失效。

## Skill 与 Prompt 接口

Skill 是项目定义的声明式场景包，计划包含清单、场景说明、Prompt、JSON Schema、Markdown 模板和脱敏评估样例。第一版不需要运行任意第三方脚本。

类目由清单中的稳定 ID、父类目、描述、适用条件、正反例定义。分类器只读启用类目的路由说明；选定后再加载完整 Skill。无法判断时进入待确认；LLM 自报置信度只作提示，不当作经过校准的概率。

允许从命令行或配置直接指定 Prompt，使用默认的结果结构；完整 Skill 可覆盖输出结构与展示模板。子类目通过显式父引用扩展；元数据子级覆盖，字段按名称合并且冲突报错，Prompt 按父→子顺序显式组合；检测循环引用，并允许配置完全覆盖 Prompt。以上规则需在实现前固化到协议版本。

| 类目 | 预期结果 |
| --- | --- |
| interview | 候选人问答复盘、表现证据、回答改进、知识缺口、岗位信息和后续行动 |
| interview.technical | 继承面试字段，扩展技术知识点、题目与解答、项目追问和练习计划 |
| consultation | 对话中的症状、时间线、既往情况、检查／用药／医嘱、说话来源和待确认项 |
| delivery | 录音中的快递信息、取送时间地点、异常和待办 |

后端结构化输出通过 Schema 校验；失败有限重试，仍失败则保留原始返回并报错。总结中的事实关联 segment ID；分析、建议、缺失信息分别标记。

## 工作区与开源实施顺序

1. 已完成：建立需求、方案、目录约定与环境启动器。
2. 已完成（当前开发机）：复制用户提供的 uv，在工作区安装受管 Python、`.venv`、依赖并写入锁文件；通过 PyAV 解码，无需安装系统 ffmpeg；未向系统 Python 安装包或修改 Shell 配置。
3. 本地 ASR/VAD、整段说话人聚类和保守归属均已完成真实录音运行；已增加中文 CTC 字符对齐估计，对低分、未支持字符与重叠语音保留不确定性，不宣称人工标注精度。
4. DeepSeek 已完成获用户明确授权的完整面试转写处理：11 个分块全部分类为技术面试，再逐块提取并合并。增加具体字段错误反馈、独立的最终输出预算、跨块角色上下文，以及禁止把 ASR 误识当作面试表现缺陷的合并规则。
5. 声明式 Skill v1、继承、Prompt 覆盖、Markdown/JSON、分类目录、SQLite 索引和分块检查点均已实现，44 项测试通过。完成一条真实面试的结构和来源编号检查；人工准确率及其他场景效果评估仍待进行。
6. 扩展与发布：补其他云端后端、跨平台 CI、英文说明、示例及许可证；再实现 API/界面。

## 官方资料

- [whisper.cpp 平台与硬件支持](https://github.com/ggml-org/whisper.cpp)
- [faster-whisper 安装与 GPU 要求](https://github.com/SYSTRAN/faster-whisper)
- [pyannote community-1 模型、访问条件、离线运行与说话人输出](https://huggingface.co/pyannote/speaker-diarization-community-1)
- [pyannote.audio 与遥测开关](https://github.com/pyannote/pyannote-audio)
- [WhisperX](https://github.com/m-bain/whisperX)
- [uv 存储目录](https://docs.astral.sh/uv/reference/storage/)
- [uv 环境变量](https://docs.astral.sh/uv/reference/environment/)
- [Hugging Face 缓存与令牌位置](https://huggingface.co/docs/huggingface_hub/package_reference/environment_variables)

资料核查日期：2026-11-07；版本和兼容性需在实际安装时固定并验证。
