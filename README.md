# 声笺 · Sonafolio

**让声音，落在纸上。**

将录音转写为带说话人、时间戳和场景结构的记录，通过可配置 Skill 整理内容。

当前已实现工作区环境、中文本地 ASR/VAD、说话人区间、保守文本归属、中文 CTC 字符对齐估计，以及可配置 Skill 分类／提取／汇总管线。已经用一条获用户授权的 72 分钟中文面试录音完成真实端到端运行：11 个分块分类为技术面试，生成候选人视角的结构化复盘和来源链接。44 项测试通过；结果仍是初版模型草稿，人工逐段准确率评估、界面和 Windows 实机验收尚未完成。

- [已确认需求](docs/requirements.md)
- [技术方案与实施顺序](docs/architecture.md)
- [工作区环境约定](docs/workspace.md)
- [场景 Skill 协议与扩展](docs/skills.md)

## 工作区

```text
sonafolio/
  docs/              需求与方案
  scripts/           环境启动器
  tests/             环境边界验证
  src/sonafolio/     处理核心与 CLI
  .venv/             项目虚拟环境
  .runtime/          项目 Python、uv、工具和运行状态
  .cache/            依赖、模型下载与框架缓存
  .models/           显式导出的模型文件
  .tmp/              临时音频和临时文件
  data/              输入、转写、类目输出、SQLite 索引和日志
```

检查环境，无第三方依赖、无网络操作：

```sh
# macOS
python3 scripts/workspace.py doctor
# Windows
py -3 scripts/workspace.py doctor
```

`doctor` 创建并展示工作区内的运行目录。上述命令中的现有 Python 只读取运行启动器，不向现有 Python 安装软件。实际应用后续使用工作区内的 Python 和 `.venv`。

环境启动器用于给子进程配置目录，业务 CLI 是 `sonafolio`；两者都不是操作系统文件沙箱。

## 安装与使用

先安装 uv，或者用 `--uv` 指定已有 uv 的路径。安装脚本将其复制到工作区，再下载 `.python-version` 指定的 Python，创建 `.venv` 并安装锁定依赖。模型与录音不会随依赖安装自动下载或上传。

```sh
# macOS；Windows 将 python3 替换为 py -3
python3 -B scripts/bootstrap.py --asr
python3 -B scripts/workspace.py run -- sonafolio --help
python3 -B scripts/workspace.py run -- sonafolio probe "data/input/recording.m4a"

# 显式下载公开 ASR 与 VAD 模型，验证 SHA-256 并记录仓库 revision
python3 -B scripts/workspace.py run -- sonafolio download-asr

# 短片段测试；--start 的单位也是秒
python3 -B scripts/workspace.py run -- sonafolio transcribe "data/input/recording.m4a" \
  --model .models/whisper.cpp/ggml-large-v3-turbo-q5_0.bin --duration 120

# 完整录音：去掉 --duration；--device cpu 可显式关闭 GPU
```

产物写入 `data/runs/<输入名称和内容指纹>/asr/<范围和设置指纹>/`：

- `transcript.json`：原始中文文本、片段时间戳、模型与参数、待处理说话人状态。
- `transcript.md`：便于阅读和回听的转写草稿。
- `status.json`、`engine.log`：阶段状态和本地引擎日志。底层进程异常退出也会记录失败。

当前 ASR 结果尚未经人工准确率评估。人名、英文术语和短应答可能出错；VAD 可能遗漏低声或重叠语音，时间戳是片段级，尚不能直接用于精确说话人归属。初次 GPU 运行可能需要编译着色器；限制 GPU 访问的沙箱中应获得运行权限或显式使用 CPU。

## 验证

```sh
python3 -B scripts/workspace.py run -- python -m unittest discover -s tests -v
```

测试覆盖目录边界、虚拟环境、实际解码与片段采样、原始输入不变、失败状态和时间戳格式。当前已在 Apple Silicon macOS 上执行；Windows 实机验证尚未完成。

2026-11-07 本地基线：一条约 72 分 47 秒的中文双声道 AAC/M4A，在当前 Apple Silicon 开发机上通过 large-v3-turbo Q5 + Silero VAD + Metal 完成转写，推理阶段约 274.8 秒，生成 2,565 个片段。原始文件 SHA-256 校验一致；说话人模块的前 120 秒测试在 CPU 上约 69.6 秒、MPS 上约 17.7 秒。自动聚类数量不等于经过人工确认的人数，尚未执行人工准确率评估。耗时不包含初次下载、安装和模型文件校验，不是其他设备的性能承诺。

整段 community-1 + MPS 说话人区分耗时约 309.0 秒，模型自动估计为 2 个声音簇；保守合并后，2,565 个 ASR 片段中 2,101 个获得单一说话人编号，464 个保留待进一步对齐。此比例衡量是否能够按当前规则归属，不代表说话人识别准确率。

后续本地说话人模型需要先在 [community-1 页面](https://huggingface.co/pyannote/speaker-diarization-community-1)接受访问条件，再执行以下命令交互输入只读令牌：

```sh
python3 -B scripts/workspace.py run -- sonafolio configure-hf
```

令牌不回显、不进入命令行参数，保存在被 Git 忽略的 `.runtime/secrets/` 中。该命令只保存令牌，不下载模型或代替用户接受条件。

说话人模块的准备和运行：

```sh
python3 -B scripts/bootstrap.py --asr --diarization
python3 -B scripts/workspace.py run -- sonafolio download-diarization
python3 -B scripts/workspace.py run -- sonafolio diarize "data/input/recording.m4a"
```

默认自动估计人数，按首次出现顺序输出 `SPEAKER_00`、`SPEAKER_01` 等，保留重叠区间与单说话人视图。编号在同一处理范围内保持一致，不代表跨录音身份。整条录音一次聚类，后续 LLM 根据各人内容推断角色；用户无需提前标注候选人。默认使用 CPU；已验证可用的 Mac 可显式加 `--device mps`。模型下载完整后，推理子进程设置离线模式并移除 Hugging Face 令牌。

将同一录音的原始转写与说话人区间合并为可读草稿：

```sh
python3 -B scripts/workspace.py run -- sonafolio annotate \
  "data/runs/<录音>/asr/<范围和设置>/transcript.json" \
  "data/runs/<录音>/diarization/<范围和设置>/speakers.json"
```

仅当片段只与一个说话人相交、且语音覆盖至少 80% 时才赋予编号。跨人发言、重叠说话或覆盖不足时保留候选编号并标记待进一步对齐，不根据最长重叠强行选人。输出的 `alignment.precision` 始终明确为 `segment`，不宣称逐词精确对齐。

## DeepSeek 接口

```sh
python3 -B scripts/workspace.py run -- sonafolio configure-deepseek
python3 -B scripts/workspace.py run -- sonafolio check-llm
```

第一条仅交互保存密钥到 `.runtime/secrets/deepseek-api-key`；也支持只在当前进程提供 `DEEPSEEK_API_KEY`。第二条会发起一个不含录音内容的微型真实请求，验证密钥、模型和 JSON 输出。暂未配置密钥时直接报错，不发起请求。

默认使用 `deepseek-flash`，显式关闭 thinking，使用 JSON Output 并在本地用 Pydantic 或场景 JSON Schema 校验。空白或结构错误最多再试一次，修复请求反馈具体字段和校验错误；持续失败的返回保留在工作区诊断文件中。认证、余额和 HTTP 错误不自动重试，不在终端打印服务端原始响应或密钥。复制 `sonafolio.example.toml` 为被 Git 忽略的 `sonafolio.toml` 可配置模型、分块输出上限（默认 8192）、超时和 thinking；最终合并默认 16384，可用 `--merge-max-tokens` 单独调整并复用分块检查点。已经完成这条真实面试的分类、提取、汇总和索引写入。

接口依据：[DeepSeek API](https://api-docs.deepseek.com/)、[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。默认模型来自当前官方文档，可通过配置更换；核查日期 2026-11-07。

## 细粒度归属与场景分析

```sh
python3 -B scripts/bootstrap.py --asr --diarization --alignment
python3 -B scripts/workspace.py run -- sonafolio download-alignment
python3 -B scripts/workspace.py run -- sonafolio refine \
  "data/runs/<录音>/annotated/<指纹>/transcript.json" \
  "data/runs/<录音>/diarization/<范围和设置>/speakers.json" --device mps

# 查看实际会发送的全部转写文本；此命令不调用 API
python3 -B scripts/workspace.py run -- sonafolio analyze \
  "data/runs/<录音>/refined/<指纹>/transcript.json" --dry-run

# 实际向 DeepSeek 发送转写文本并生成场景总结
python3 -B scripts/workspace.py run -- sonafolio analyze \
  "data/runs/<录音>/refined/<指纹>/transcript.json"
```

CTC 对齐使用公开中文 Wav2Vec2 模型，默认只处理归属不清的片段。模型支持的字符通过单调 CTC 路径估计时间，再与说话人区间合并；低分字符、未支持术语和重叠语音仍保留待确认，标点可以继承相邻字的展示位置，原始 ASR 文字不得改变。它不是人工验证的精确时间标注，也不会纠正 ASR 识别内容。

当前样本已对 464 个原始待确认片段完成该阶段（约 39.1 秒）；输出保留全部原文，仍有 2,102 个字符未可靠归属，不能把赋予编号的比例当作准确率。模型与推理临时文件都保存在工作区。该中文模型是 [WhisperX 中文默认对齐模型所采用的检查点](https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py)，具体模型及许可证见 [模型页](https://huggingface.co/jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn)。

`analyze` 对完整转写分块分类，按所选 Skill 提取各块，再合并；每个块保留检查点，角色由 LLM 根据内容推断。输出含来源链接和 SQLite 索引；只改 Prompt、模板或 Skill 后可复用已有 ASR 和声音聚类结果。当前模板覆盖通用面试、技术面试、HR 面试、问诊、快递和未知场景，详见 [Skill 协议](docs/skills.md)。

最终合并包含跨块角色观察和少量原文样例，避免因候选人反问而互换角色；不得把 ASR 错误当作候选人能力缺陷，也不得把笔记遗漏当作原录音没有说。结构、编号及少量人工抽查不能证明内容准确；技术术语、数字、评价与声音归属仍需回听核对。其他场景目前提供模板和协议，尚未用真实样本完成效果评估。

## 开源准备

计划提供中文和英文文档、声明式 Skill 协议、脱敏示例、macOS/Windows CI、依赖锁文件及模型来源说明。项目代码许可证待选择；模型遵循各自许可证，不随源码直接分发权重。真实录音、令牌、运行环境和模型缓存不进入源码版本管理。
