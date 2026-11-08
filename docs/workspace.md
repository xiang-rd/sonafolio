# 开发与运行

本文是安装、命令、密钥和环境边界的运行手册。当前完成情况见 [status.md](status.md)，阶段产物结构见 [architecture.md](architecture.md)。

## 环境边界

应用依赖只安装到 `.venv`；Python、uv 与本地工具放 `.runtime`；模型和缓存放 `.models`、`.cache`；临时音频放 `.tmp`；业务数据放 `data`。不使用 sudo、全局 pip、pip --user 或全局模型安装，不修改 Shell profile、系统 PATH、注册表或现有 Python 环境。

`scripts/workspace.py` 以脚本位置定位工作区，为子进程配置目录，不改变父进程环境，不重设 HOME、USERPROFILE 或 CODEX_HOME。目录经符号链接逃逸工作区时拒绝执行。uv、Python、pip 与 Sonafolio 的标准启动入口只使用工作区工具，不回退到全局安装。

启动器覆盖 uv / pip / Hugging Face / Torch / Numba / Matplotlib / XDG / 临时目录等配置，禁用用户 site、外部 PYTHONPATH、隐式 Hugging Face 令牌和相关可选遥测；uv 忽略外部配置。新后端须在导入模型库前设置下载和缓存路径，并验证实际写入位置。后续本地 LLM 服务也应使用专属进程和工作区模型目录。

这是应用级目录约定，**不是操作系统文件沙箱**。第三方程序、OS 日志和交换文件不能仅靠环境变量约束；现有验证不等于对所有第三方依赖完成系统级写入审计。启动器允许运行其他显式命令，不自动保证任意程序遵守这些边界。

## 初始化与诊断

下列命令在项目根目录执行。macOS 用 `python3`，Windows 用 `py -3` 替换命令开头；启动脚本要求现有 Python 3.10+，应用使用 `.python-version` 指定的专用 Python 3.12。不会向启动脚本所用的现有 Python 安装包。

```sh
# 仅创建和检查目录，不下载
python3 -B scripts/workspace.py doctor

# 读取已有 uv，复制进工作区；可加 --uv 指定已有可执行文件
python3 -B scripts/bootstrap.py --asr --diarization --alignment

python3 -B scripts/workspace.py run -- uv --version
python3 -B scripts/workspace.py run -- sonafolio --help
```

bootstrap 下载受管 Python，创建虚拟环境，按 `uv.lock` 安装依赖。pywhispercpp 仅使用预编译 wheel，没有匹配 wheel 时失败，不隐式进行源码编译。安装依赖不等于下载模型，也不会自动发送录音。

## 密钥与模型准备

```sh
# 交互输入不回显；仅保存，不请求模型
python3 -B scripts/workspace.py run -- sonafolio configure-hf
python3 -B scripts/workspace.py run -- sonafolio configure-deepseek

# 以下命令会联网下载到工作区
python3 -B scripts/workspace.py run -- sonafolio download-asr
python3 -B scripts/workspace.py run -- sonafolio download-diarization
python3 -B scripts/workspace.py run -- sonafolio download-alignment
```

说话人模型下载前，用户需用创建令牌的账号在 [community-1 模型页](https://huggingface.co/pyannote/speaker-diarization-community-1)接受访问条件，并配置有权读取该模型的令牌。项目不代替用户同意条件；模型准备好后，ASR、说话人区分和对齐使用本地权重。下载清单记录来源和版本／校验信息。

凭据保存在被 Git 忽略的本地明文文件：

| 文件 | 用途 |
| --- | --- |
| `.runtime/secrets/huggingface-token` | 下载受限 Hugging Face 模型 |
| `.runtime/secrets/deepseek-api-key` | DeepSeek 请求 |

POSIX 系统保存时设置 `0600` 权限；Windows 凭据访问控制尚需单独验收。DeepSeek 也支持当前进程的 `DEEPSEEK_API_KEY`，其优先于文件；无需写入全局环境变量。`.env` 暂不自动加载。不要使用全局 `hf auth login`，不要在聊天、命令行参数或日志中粘贴密钥。

复制 `sonafolio.example.toml` 为被忽略的 `sonafolio.toml` 可配置 LLM 模型、输出预算、超时和 thinking。当前只实现 DeepSeek，默认 `deepseek-flash`，分块输出 8192、最终合并 16384；后者用 `--merge-max-tokens` 调整。模型可用性以配置服务实际返回为准。

## 业务数据与备份

当前 CLI 的业务数据默认根目录为工作区内的 `data/`，与 `.models/`、`.cache/` 分开：

```text
data/
  input/          原始录音，处理时只读
  runs/           每条录音的元信息、转写、说话人、对齐及 LLM 检查点
  output/         按场景、运行版本保存总结 JSON、Markdown 和来源页
  index.sqlite    已完成分析的索引，目前不是完整任务数据库
  validation/     本地私有验证记录
```

桌面原型“设置 → 存储位置”已增加“录音与结果目录”，默认 `data`，显示相对于工作区解析后的完整路径。数据、模型、缓存必须在工作区内且不能相同或相互嵌套。此设置目前仅在页面内存中演示，CLI 尚未接入可配置的数据根目录，也没有迁移任何已有文件；浏览器路径校验不能替代后端的符号链接和实际路径校验。

正式接入时，所有阶段、导出与索引须统一从数据根目录解析路径。桌面导入计划复制原始录音到受管目录，处理不覆盖原音频；当前 CLI 也能读取外部输入，不代表已实现自动复制。显示标题与录音身份应分离，重命名不能使已有来源引用失效。变更目录和迁移已有数据是两个操作：迁移须校验文件指纹、更新元信息和索引中的路径，失败时保留旧数据，不能仅替换配置字符串。

备份前结束写入任务，备份 `data/`、自定义 `skills/`（如有）及本地 `sonafolio.toml`；密钥单独安全保管，不随开源代码或普通分享包分发。模型和下载缓存可重建，不必每次纳入业务备份。当前录音、结果和密钥均被 Git 忽略，Git 提交不能代替数据备份。

## 当前分阶段运行

输入放 `data/input/`，原始音频只读。以下 `<录音>`、`<范围和设置>`、`<指纹>` 是占位符，按每条命令返回的 `output_directory` 填入下一阶段路径。当前没有统一的 `sonafolio run` 命令。

```sh
python3 -B scripts/workspace.py run -- sonafolio probe "data/input/recording.m4a"
python3 -B scripts/workspace.py run -- sonafolio transcribe "data/input/recording.m4a" --model .models/whisper.cpp/ggml-large-v3-turbo-q5_0.bin
python3 -B scripts/workspace.py run -- sonafolio diarize "data/input/recording.m4a"
python3 -B scripts/workspace.py run -- sonafolio annotate "data/runs/<录音>/asr/<范围和设置>/transcript.json" "data/runs/<录音>/diarization/<范围和设置>/speakers.json"
python3 -B scripts/workspace.py run -- sonafolio refine "data/runs/<录音>/annotated/<指纹>/transcript.json" "data/runs/<录音>/diarization/<范围和设置>/speakers.json"
python3 -B scripts/workspace.py run -- sonafolio list-skills

# 仅生成发送计划和完整文本预览，不调用 API
python3 -B scripts/workspace.py run -- sonafolio analyze "data/runs/<录音>/refined/<指纹>/transcript.json" --dry-run

# 这条会向 DeepSeek 发送转写文本、时间戳、说话人和来源 ID、场景指令
python3 -B scripts/workspace.py run -- sonafolio analyze "data/runs/<录音>/refined/<指纹>/transcript.json"
```

ASR 和 diarize 可用 `--start`、`--duration` 做同一范围的短样本；仅一端截短会导致后续范围不匹配。ASR 的 `--device cpu` 关闭 GPU，默认 `auto`。diarize / refine 默认 CPU；Mac MPS 需显式 `--device mps`，CUDA 等设备须在目标环境另行验证。说话人数默认自动估计，不要求提前标注候选人。

`sonafolio check-llm` 会发送一个不含录音的微型真实请求，用于验证连接；常规离线检查无需执行。真实 analyze 不上传原始 M4A 或 HF 令牌，不自动切换供应商。Prompt、强制类目、缓存和输出说明见 [skills.md](skills.md)。

## 验证与构建

```sh
python3 -B scripts/workspace.py run -- python -m unittest discover -s tests -v
python3 -B scripts/workspace.py run -- uv lock --check --offline
python3 -B scripts/workspace.py run -- uv build --offline
git diff --check
```

自动化测试使用项目依赖、合成音频和模拟模型／网络，不需要真实模型和密钥；环境启动器的测试只使用标准库。离线锁文件与构建检查要求所需依赖已缓存，首次准备环境仍可能需要网络。测试通过不等于真实模型效果或 Windows 实机通过。

构建产物在 `dist/`，发布前检查 wheel / sdist 包含内置 Skill、源码包包含交接文档，且未包含 `data`、模型、环境和密钥。代码许可证尚待选择，当前构建成功不代表具备正式发布条件。

## 清理与迁移

`.runtime`、`.venv`、`.cache`、`.models`、`.tmp`、`data` 和 `dist` 均忽略版本管理。清理前备份输入、结果和本地密钥，只删除明确可重建的目录；不要整目录清空用户数据。

迁移机器或更换项目路径后重建 Python／虚拟环境，不直接搬运 `.venv`。模型可单独迁移并校验版本；现有音频元信息和 SQLite 结果可能含绝对路径，还需迁移或重新建立索引。目前尚无通用的一键迁移命令。
