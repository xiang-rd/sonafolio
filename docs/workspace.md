# 工作区环境约定

## 边界

应用依赖只安装到 `.venv`；Python、uv 与本地工具放 `.runtime`；模型及缓存放 `.models` 与 `.cache`；音频临时文件放 `.tmp`；业务数据放 `data`。源码外不写入项目管理的环境与数据。

不使用 `sudo`、全局 pip、`pip --user`、Homebrew 安装或全局模型服务安装，不修改 Shell profile、系统 PATH、注册表、现有 Python 环境及全局登录令牌。后续本地 LLM 若采用 Ollama 等服务，要明确启动专属进程并指定工作区模型目录，不能直接复用它的默认全局目录。

`scripts/workspace.py` 复制当前进程环境，再对子进程覆盖相关目录变量；不改父进程环境，不重设 HOME、USERPROFILE 或 CODEX_HOME。以脚本位置定位工作区，不依赖调用者当前目录。发现运行目录经符号链接逃逸工作区时拒绝执行。

覆盖目录包括 uv 的依赖缓存、受管 Python、Python 下载缓存、工具目录和工具入口，pip 缓存与配置，Hugging Face 的模型／Xet／assets 缓存与令牌路径，Torch、Numba、Matplotlib 缓存，XDG 路径及临时目录。禁用 Python 用户 site、隐式 Hugging Face 令牌和相关可选遥测。新适配器必须在导入模型库之前设置自身的显式下载路径，并补做写入位置检查。

环境变量属于应用级目录配置，**不是操作系统级文件访问限制**；不能阻止任意第三方程序自行写入其他路径。因此每个真实模型适配器还须实测其下载、加载与运行的写入位置。当前已验证启动器、解码和文件输出边界，ASR/VAD 使用显式本地模型路径，并将推理日志和临时采样写入工作区；这不等同于对所有第三方后端做了操作系统级写入审计。操作系统自身的日志、交换文件等不属于应用可控制的目录。

## 使用

macOS 使用 `python3`，Windows 可使用 `py -3` 调用本启动器。需要 Python 3.10 或以上；此时只读现有解释器，不安装包。

```sh
python3 scripts/workspace.py doctor
python3 scripts/workspace.py run -- uv --version
```

第二条只会查找 `.runtime/bin/uv`（Windows 为 `uv.exe`），未安装时明确报错，不回退到全局 uv。`run -- python ...`、`python3` 和 `pip` 同理仅使用 `.venv` 中的 Python，其中 `pip` 展开为 `python -m pip`；`run -- sonafolio` 通过虚拟环境 Python 启动应用。

实际安装入口为 `python3 -B scripts/bootstrap.py --asr`（Windows 使用 `py -3`）。它只读用户已有 uv 并复制到 `.runtime/bin`，随后将项目 Python、依赖与缓存安装在工作区。现有锁文件通过 `uv sync --locked` 验证。ASR 仅安装预编译 wheel，不隐式进行 native 源码编译；没有匹配 wheel 时明确失败。当前开发机已完成此安装并下载公开 ASR/VAD 模型。

启动器也允许运行显式给定的其他命令，为它们提供相同子进程环境；这不是安全沙箱，直接执行任意命令并不自动保证其写入行为符合约定。项目正式入口和安装流程后续必须使用本地 Python／工具的显式路径。

uv 设为忽略外部配置，Python 设为不读取用户 site 和外部 PYTHONPATH；包索引和镜像后续通过项目显式配置传入。脚本不会打印完整环境、API key 或 Hugging Face token。`.env` 暂不自动加载；Hugging Face 令牌使用 `sonafolio configure-hf` 交互保存到工作区，不调用全局 `hf auth login`。

验证启动器：

```sh
python3 scripts/workspace.py run -- /absolute/path/to/python3 -B -m unittest discover -s tests -v
```

Windows 将解释器替换为可读取的实际绝对路径；正式环境建立后使用 `run -- python -B -m unittest discover -s tests -v`。测试只使用标准库，不访问网络或模型。该验证不等同于 Windows 实机通过。

## 清理与迁移

`.runtime`、`.venv`、`.cache`、`.models`、`.tmp` 与 `data` 均忽略版本管理；不要把真实输入音频或令牌提交到 Git。清理环境时先备份 `data` 和本地密钥，再按需删除生成目录。迁移机器时重建 Python／虚拟环境，不能直接搬运 `.venv`；模型可以单独迁移并校验版本。
