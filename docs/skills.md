# Sonafolio 场景 Skill 协议 v1

这是项目自己的声明式场景格式，用于分类和内容整理。它不执行 Python、Shell 或其他插件代码，不宣称兼容其他 Agent 的 Skill 运行时。

## 文件

每个场景一个目录：

```text
my-scene/
  skill.toml
  prompt.md
  schema.json
  template.md.j2
```

清单示例：

```toml
protocol_version = 1
id = "interview.custom"
version = "1.0.0"
parent = "interview"
description = "用于分类路由的场景描述，说明何时应选这个类目。"
enabled = true
```

- ID 使用小写字母、数字、点和连字符；以字母开头。
- 内置包位于 `src/sonafolio/builtin_skills/`，随 Python 包分发。
- 默认读取工作区 `skills/` 中的自定义包，也可通过 `--skills-dir` 指定目录。
- 同一个自定义目录内出现重复 ID 会报错；工作区同 ID 可以整体覆盖内置定义。
- `description` 提供给分类器；完整 Prompt 只在类目选定后用于提取。
- `general` 是禁用路由的基础定义；`unknown` 是无法确定分类时的保底场景。

## 继承

- 元数据由子级清单明确指定。
- Prompt 按父级、子级顺序组合。
- Schema 的 `properties`、`$defs` 按名称合并，定义冲突时报错；`required` 取并集。
- 子级未提供模板时继承父级模板。
- 缺失父级、循环继承、非法 JSON Schema 和目录逃逸都会报错。
- 改为完全独立的输出结构时，省略 `parent`，提供完整 Prompt、Schema 和模板。

Schema 必须描述 JSON object，采用 Draft 2020-12。每次返回先校验结构，再检查来源编号和说话人编号。内置场景区分 `facts`、`analysis`、`actions` 和 `uncertainties`；每条事实、分析、行动使用 `segment_ids` 引用转写依据。角色属于基于内容的推断，未知角色保持 `unknown`。

模板使用 Jinja2 沙箱及 StrictUndefined；变量为 `result`、`metadata`，辅助函数 `refs(segment_ids)` 生成来源链接，`role_name(role)` 显示中文角色名。模板缺少字段会明确报错。

## 运行

```sh
python3 -B scripts/workspace.py run -- sonafolio list-skills
python3 -B scripts/workspace.py run -- sonafolio analyze path/to/transcript.json --dry-run
python3 -B scripts/workspace.py run -- sonafolio analyze path/to/transcript.json
python3 -B scripts/workspace.py run -- sonafolio analyze path/to/transcript.json --skill interview.technical
python3 -B scripts/workspace.py run -- sonafolio analyze path/to/transcript.json --prompt prompts/my-prompt.md
```

`--prompt` 用文件内容替换所选 Skill 的提取指令；仍使用所选 Skill 的输出结构、模板和来源校验。需要改变结构时新增独立 Skill。

## 长录音、分类与缓存

先将完整转写按原始片段分块；超限片段会报错，不截断丢弃。所有块都参与分类，通过 60% 支持阈值选择明确的子类目或共同父类目，无明显主类目时进入 `unknown`。这是可解释的路由规则，不是校准后的分类概率。

随后逐块按选定 Skill 提取，最后合并分块笔记。中间结果写入输入对应运行目录的 `analysis/<设置指纹>/`。修改输入、Skill、Prompt 或模型设置会得到新的指纹；重跑默认复用通过校验的中间结果，`--force` 才重做模型请求。

最终合并的输出预算默认 16384，独立于分块提取的 8192 默认值；使用 `--merge-max-tokens` 调整，只生成新的合并版本和结果目录，复用已通过的分类与提取检查点。具体结构错误会反馈给模型修复；未通过的返回保留为 `.failed.json` 本地诊断。旧诊断或旧合并版本可以保留用于比较，最终状态以 `status.json` 和当前结果目录为准。

`--dry-run` 仅写入本地发送计划和完整文本预览，不调用 API。真实 `analyze` 会把转写文本、片段编号、时间戳、声音编号和场景指令发送给配置的 DeepSeek 服务。当前不会向 DeepSeek 上传原始音频，也不会向其发送 Hugging Face 令牌。

结果按类目写入 `data/output/<主类目>/<子类目>/<运行标识>/`，包含 `summary.md`、`summary.json` 和带来源锚点的 `sources.md`。SQLite 索引位于 `data/index.sqlite`。模型推断结果仍需回听复核，Schema 与来源编号校验不等于事实准确率验证。
