# 开发与运行

本文是安装、命令、密钥和环境边界的运行手册。当前完成情况见 [status.md](status.md)，阶段产物结构见 [architecture.md](architecture.md)。

## 环境边界

应用依赖只安装到 `.venv`；Python、uv 与本地工具放 `.runtime`；模型和缓存放 `.models`、`.cache`；临时音频放 `.tmp`；业务数据放 `data`。不使用 sudo、全局 pip、pip --user 或全局模型安装，不修改 Shell profile、系统 PATH、注册表或现有 Python 环境。

`scripts/workspace.py` 以脚本位置定位工作区，为子进程配置目录，不改变父进程环境，不重设 HOME、USERPROFILE 或 CODEX_HOME。目录经符号链接逃逸工作区时拒绝执行。uv、Python、pip 与 Sonafolio 的标准启动入口只使用工作区工具，不回退到全局安装。

启动器覆盖 uv / pip / Hugging Face / Torch / Numba / Matplotlib / XDG / 临时目录等配置，禁用用户 site、外部 PYTHONPATH、隐式 Hugging Face 令牌和相关可选遥测；uv 忽略外部配置。新后端须在导入模型库前设置下载和缓存路径，并验证实际写入位置。后续本地 LLM 服务也应使用专属进程和工作区模型目录。

这是应用级目录约定，**不是操作系统文件沙箱**。第三方程序、OS 日志和交换文件不能仅靠环境变量约束；现有验证不等于对所有第三方依赖完成系统级写入审计。启动器允许运行其他显式命令，不自动保证任意程序遵守这些边界。

## 初始化与诊断

下列命令在项目根目录执行。macOS 用 `python3`，Windows 用 `py -3` 替换命令开头；启动脚本要求现有 Python 3.11+，应用使用 `.python-version` 指定的专用 Python 3.12。不会向启动脚本所用的现有 Python 安装包。

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

业务数据默认根目录是工作区内的 `data/`，新录音原始文件、阶段结果及各分析版本聚合在 `runs/<任务 ID>/`，全局分析索引为 `index.sqlite`。详细结构见[任务数据布局](architecture.md#任务数据布局)。现有 `data/input/`、`data/output/` 和旧运行目录没有自动迁移，`validation/` 私有基线继续保留。

`probe` 和音频阶段会导入录音：创建受管副本并核对 SHA-256，源文件不改动；重复导入同内容复用同一任务。任务 ID 与显示标题分离；`rename-task` 只改标题，不改受管音频文件名和来源引用。

在本地 `sonafolio.toml` 中设置 `[storage] data_dir = "data"` 可修改 CLI 的数据根目录；必须是工作区内专用相对目录，不得占用模型、缓存、密钥、环境、源码或场景目录，符号链接解析后仍须在工作区内。目录自动写入忽略业务内容的 `.gitignore`，避免自定义目录中的私有文件进入 Git。模型和运行缓存继续由工作区启动器管理。浏览器原型设置仍只保存在内存；真实桌面的存储设置已连接该文件。真实桌面可编辑数据、模型和缓存目录；在“检查并应用”中选择使用已有目录或复制迁移，复制校验完成后才切换配置，原目录保留。手动编辑 TOML 仅切换路径，不搬迁文件。

变更配置不会搬迁已有数据。当前提供旧布局到新任务结构的复制迁移，不提供任意两数据根目录间的一键搬迁。迁移前备份，迁移失败保留旧文件；不通过替换路径字符串代替实际复制与引用校验。

备份前结束写入任务，备份数据根目录、自定义 `skills/`（如有）和 `sonafolio.toml`。单个完整任务目录可独立备份，但还原到另一工作区后仍需重建索引；自动任务包导入尚未提供，旧诊断文件中的绝对路径不作为新任务身份依据。密钥单独安全保管，不随源码或普通分享包分发；模型和下载缓存可重建。Git 不包含业务数据，提交不能代替备份。

## 统一运行与恢复

完成模型准备后，在项目根目录执行（Windows 将 `python3` 替换为 `py -3`）：

```sh
# 默认本地处理并生成待发送文本预览，不请求 LLM
python3 -B scripts/workspace.py run -- sonafolio run "/path/to/recording.m4a"

# 明确调用 DeepSeek 分类与总结；仅发送文字、编号、时间和场景指令
python3 -B scripts/workspace.py run -- sonafolio run "/path/to/recording.m4a" --analysis cloud

# 仅转写与说话人；可跳过 CTC 细化，但保留不可靠归属
python3 -B scripts/workspace.py run -- sonafolio run "/path/to/recording.m4a" --analysis none --no-refine

# 沿用保存的选项续跑；默认保留原来的云端／预览模式
python3 -B scripts/workspace.py run -- sonafolio resume <任务ID>
# 将已生成预览的任务切换为真实分析
python3 -B scripts/workspace.py run -- sonafolio resume <任务ID> --analysis cloud

python3 -B scripts/workspace.py run -- sonafolio rename-task <任务ID> "新的录音标题"
python3 -B scripts/workspace.py run -- sonafolio rebuild-index

# 旧运行目录先预览；--apply 才复制迁移，始终保留旧文件
python3 -B scripts/workspace.py run -- sonafolio migrate-run "data/runs/<旧运行目录>"
python3 -B scripts/workspace.py run -- sonafolio migrate-run "data/runs/<旧运行目录>" --apply
```

同一音频再执行 `run` 会校验并复用已完成阶段；改变参数会使相关阶段失效，`--force` 为所有阶段创建新尝试。Ctrl+C 会记录取消并终止子进程树，下次可续跑；硬终止后也可重试，但未完成的音频阶段需要重算。进度事件写 stderr，最终结果 JSON 写 stdout；诊断日志保存在任务阶段目录。

迁移将旧阶段和总结汇集到新任务，检查音频指纹、转写路径、来源引用和索引；旧规则快照不存在时明确记录缺失。迁移内容保留为历史版本，不自动视为新 Pipeline 的可复用缓存，重新执行 `run` 可能重算。此轮未自动迁移开发机真实数据。

## 当前分阶段运行

仍可独立调用各阶段。输入可为外部文件或旧 `data/input/` 中的录音，都会导入受管副本；以下使用旧输入路径演示，`<任务ID>`、`<ASR版本>`、`<说话人版本>`、`<归属版本>`、`<细化版本>` 为占位符，按实际命令返回的 `output_directory` 传递路径。

```sh
python3 -B scripts/workspace.py run -- sonafolio probe "data/input/recording.m4a"
python3 -B scripts/workspace.py run -- sonafolio transcribe "data/input/recording.m4a" --model .models/whisper.cpp/ggml-large-v3-turbo-q5_0.bin
python3 -B scripts/workspace.py run -- sonafolio diarize "data/input/recording.m4a"
python3 -B scripts/workspace.py run -- sonafolio annotate "data/runs/<任务ID>/asr/<ASR版本>/transcript.json" "data/runs/<任务ID>/diarization/<说话人版本>/speakers.json"
python3 -B scripts/workspace.py run -- sonafolio refine "data/runs/<任务ID>/alignment/<归属版本>/transcript.json" "data/runs/<任务ID>/diarization/<说话人版本>/speakers.json"
python3 -B scripts/workspace.py run -- sonafolio list-skills

# 仅生成发送计划和完整文本预览，不调用 API
python3 -B scripts/workspace.py run -- sonafolio analyze "data/runs/<任务ID>/alignment/<细化版本>/transcript.json" --dry-run

# 这条会向 DeepSeek 发送转写文本、时间戳、说话人和来源 ID、场景指令
python3 -B scripts/workspace.py run -- sonafolio analyze "data/runs/<任务ID>/alignment/<细化版本>/transcript.json"
```

ASR 和 diarize 可用 `--start`、`--duration` 做同一范围的短样本；仅一端截短会导致后续范围不匹配。ASR 的 `--device cpu` 关闭 GPU，默认 `auto`。diarize / refine 默认 CPU；Mac MPS 需显式 `--device mps`，CUDA 等设备须在目标环境另行验证。说话人数默认自动估计，不要求提前标注候选人。

`sonafolio check-llm` 会发送一个不含录音的微型真实请求，用于验证连接；常规离线检查无需执行。真实 analyze 不上传原始 M4A 或 HF 令牌，不自动切换供应商。Prompt、强制类目、缓存和输出说明见 [skills.md](skills.md)。

## 桌面桥接调试

以 macOS 为当前验收基准，Windows 实机验证暂缓。桌面宿主后续启动以下进程，并持续读写其管道：

```sh
python3 -B scripts/workspace.py run -- sonafolio ipc
# 导出请求、方法参数及输出消息的 JSON Schema，无模型／网络调用
python3 -B scripts/workspace.py run -- sonafolio ipc --schema > .tmp/ipc-schema.json
```

首条输出为 `server.ready`。每次请求使用新的 ID，例如 `{"protocol_version":1,"id":"req-1","method":"hello","params":{}}`。任务启动后先收到接受响应，再收到进度与最终事件；查询、取消和读取结果通过同一通道进行，字段与限额见[协议说明](architecture.md#本地桌面协议-v1)。关闭 stdin 会取消当前任务并等待退出，发送启动请求后应保持管道开放直到终态。单次人工处理继续使用 `run` 命令。

stdout 只输出协议帧，诊断在 stderr 或任务日志；错误消息不会回显 API Key、请求原文或异常正文。HTML 原型保持独立；真实 Tauri / React 应用通过此入口连接包内 Python。

## macOS 桌面与独立运行包

当前交付是 macOS Apple Silicon 开发版，目标 macOS 14+，本机验证；不是经过 Developer ID 签名与公证的公开安装包。Windows 实机继续暂缓。应用输出为 `dist/macos/Sonafolio.app`，约 1.12 GiB，不包含模型、录音或密钥。

```sh
open "dist/macos/Sonafolio.app"
```

启动后手动选择工作区；当前不在全局位置保存“上次工作区”。选择现有项目目录可沿用本地 `.models/` 和凭据。新工作区可在“设置 → 本地模型”中下载并校验 ASR、语音活动检测、说话人和中文对齐组件；也可继续使用前文 CLI。说话人模型需先在 Hugging Face 页面取得访问权限，再在界面保存只读令牌；只显示配置状态，不回显令牌。下载可以取消、重试并复用已下载的临时文件，完整性校验通过后才安装。模型、缓存及数据目录均可编辑，且须留在工作区内、互不重叠；“使用目标目录”不搬迁数据，“复制迁移”只接受空目标，原目录会保留。真实界面支持场景编辑、版本保存、模板预览与关键词 AI 起草；草稿确认启用后才自动匹配。导入批次写入数据目录下 `batches/<UUID>/`，重启后选择原工作区即可继续；原音频只存任务 `runs/<SHA>/input/`。播放栏支持暂停、拖动、倍速，点击逐字稿时间点定位。新录音的云端整理与批次“生成待处理总结”需在界面确认发送文字；AI 起草弹窗明确发送关键词与基础模板。

构建需要 Xcode Command Line Tools、Node 和项目内 Rust；不修改系统工具链、Shell 或全局包。Rust 固定版本见 `desktop/rust-toolchain.toml`，Cargo 与 npm 依赖分别由两个锁文件固定。首次在工作区安装 Rust，可从[官方 rustup](https://rustup.rs/)下载安装脚本到 `.tmp` 后，以 `RUSTUP_HOME="$PWD/.runtime/rustup" CARGO_HOME="$PWD/.runtime/cargo"` 执行，使用 `--no-modify-path --profile minimal --default-toolchain 1.99.1`。后续命令统一通过桌面启动器：

```sh
# 锁定的 Python 依赖先按前文 bootstrap 准备
python3 -B scripts/desktop.py -- npm ci
# 首次缺少依赖缓存时加 --online；全程按 uv.lock 校验哈希
python3 -B scripts/workspace.py run -- python scripts/build_macos_runtime.py --online
# 默认离线构建；只改一方代码时可以快速刷新应用 wheel 与启动器
python3 -B scripts/workspace.py run -- python scripts/build_macos_runtime.py --refresh-code
python3 -B scripts/desktop.py -- npm run build
python3 -B scripts/desktop.py -- npm test
python3 -B scripts/desktop.py -- cargo check --manifest-path src-tauri/Cargo.toml
python3 -B scripts/desktop.py -- npm exec tauri build -- --debug --bundles app
```

完整构建也可省略 `--online` 使用已有缓存；依赖锁变更后不能仅 refresh-code。运行包在 `build/macos-runtime/`，Tauri 输出 `.cache/cargo-target/debug/bundle/macos/Sonafolio.app`；交付时复制到 `dist/macos/`。这些目录均忽略 Git。`scripts/desktop.py` 将 Rust 工具链、Cargo target、npm 与编译缓存限制在项目内；不会向全局安装 npm 包或修改 rustup 默认配置。

运行不需要系统 Python。包内 `runtime/launch.py` 默认进入 IPC，也可在设置 `SONAFOLIO_WORKSPACE` 后转发普通 CLI 参数（例如模型下载），由内置 Python 以 `-I -B` 执行；`--check` 检查全部原生后端、TorchCodec 解码及 dyld 外部库依赖。它在模型导入之前应用工作区环境，不创建或搬运 `.venv`。记录运行包与模型版本，升级代码后按阶段签名决定复用；长录音、多人准确率和 Metal 性能不由单个合成样本验证替代。

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

迁移机器或更换项目路径后重建 Python／虚拟环境，不直接搬运 `.venv`。模型可单独迁移并校验版本；现有音频元信息和 SQLite 结果可能含绝对路径，还需迁移或重新建立索引。旧运行结构有显式复制迁移命令；跨机器完整环境和任意数据根目录搬迁仍无通用一键命令。

### GitHub 发布方向

源码计划托管 GitHub，运行包通过 GitHub Releases 分发；当前未关联远程或发布 release。现有 `.app` 仍为本地开发构建。当前采用无需 Apple Developer Program 的测试版方案：ad-hoc 临时签名，不公证。首次下载可能被 Gatekeeper 拦截，不能承诺仅出现普通的“是否打开”确认。公开发布前仍需选择许可证、检查模型／依赖再分发条款，并进行全新机器安装验收；将来有开发者账号后再升级 Developer ID 签名与公证。不要把本地工作区、密钥、模型或私有录音打进发行资产。

模型下载临时文件位于所选缓存目录 `model-downloads/<组件>/<revision>/`，取消后保留供重试。组件安装的历史备份位于所选模型目录 `.history/`；不会自动删除旧安装。全量校验可能需要读取较大的权重文件。下载／迁移与录音推理互斥；数据目录复制后，任务恢复会从新任务内解析目录／Prompt 快照。运行包升级后重新打开应用，无需重装 Python 或依赖。

“本地模型”页可选择说话人计算设备：自动模式在 Apple GPU（MPS）可用时使用 GPU，否则 CPU；CPU 可手动指定。CLI 使用工作区 `[processing].speaker_device` 默认值，也可用 `run --speaker-device` 或 `diarize --device` 明确覆盖。批次取消后修改设备，再继续处理会复用 ASR，只重新计算依赖该设备的阶段。字符对齐目前仍默认 CPU。

### 无开发者账号的 macOS 测试包

先完成上方 `.app` 构建，再运行（Python 3.11+）：

```sh
python3 -B scripts/package_macos.py
```

默认读取 `dist/macos/Sonafolio.app`；也可用 `--app` 指定工作区内另一个 `.app`（例如 release 构建的输出）。脚本在临时副本中逐个签名 Mach-O 文件，再签名外层应用；不修改源应用，不访问钥匙串，不需要证书或联网。随后生成 ZIP，解压回验 `codesign --verify --deep --strict`，计算 SHA-256。已有同版本产物时拒绝覆盖，保留旧包后再重新打包。临时签名只验证代码完整性，没有 Apple 认证身份，不等于 Developer ID 签名或公证。

产物在 `dist/releases/Sonafolio-<版本>-dev-macos-arm64/`：ZIP、`SHA256SUMS.txt`、`FIRST-OPEN.txt` 与 `release.json`。ZIP 内含应用与打开说明，目标 Apple Silicon、macOS 14+。包内无用户录音、API 凭据或 ASR／说话人模型；第三方依赖可能包含其公共样例和辅助资源。打包器检查常见私有目录、配置与逃逸符号链接，不替代发行前依赖／许可证审查。

用户首次打开：解压并将应用拖入“应用程序”，尝试打开；若提示无法验证开发者或无法检查恶意软件，关闭提示，进入“系统设置 → 隐私与安全 → 仍要打开”，再按系统要求确认。只在确认下载来源可信时放行；受管理设备可能禁止此操作。系统提示随 macOS 版本变化，参见 [Apple 首次打开说明](https://support.apple.com/102445)。无需关闭全局 Gatekeeper，也不提供自动移除隔离属性的安装脚本。

GitHub Releases 后续使用预发布（Pre-release），标题明确标注“macOS 测试版／未公证”，同时提供 ZIP、校验值与打开说明。当前脚本只在本地生成文件，不创建远程仓库、不上传或发布。现有默认输入为 debug 开发构建，文件名的 `dev` 不代表优化后的正式发行版。
