# 纯血鸿蒙机会雷达 (Harmony Opportunity Radar)

以 Apple App Store 榜单为入口，寻找「Apple 已验证、鸿蒙侧可能存在供给落差」的机会假设，并用四维审计打分输出决策看板。

> 定位说明：本工具产出的是**待验证的机会假设**，而非已证实的全市场结论。所有打分基于输入事实，不虚构下载量、收入、评分趋势或平台能力。

## 功能概览

流水线分为三个阶段，逐级依赖上游产物：

| 阶段 | 命令 | 作用 |
|---|---|---|
| capture | `python -m radar capture` | 拉取 Apple 榜单 → LLM 精选标杆 → 提炼 JTBD |
| probe | `python -m radar probe` | 检索鸿蒙同类供给，两阶段过滤竞品并抓取中差评 |
| audit | `python -m radar audit` | 四维打分审计，筛选 ≥6.0 分的高潜机会 |

每个阶段都会在 `data/runs/<batch_id>/` 下新建批次目录，归档中间产物、提示词快照与 LLM traces，互不覆盖。

## 环境要求

- Python 3.11+（依赖标准库 `tomllib`）
- Windows 环境可使用 `run.ps1` / `run.bat`；其他平台可直接使用 `python -m radar`

## 安装

```powershell
# 安装依赖
.\run.ps1 install
# 或
python -m pip install -r requirements.txt
```

## 配置

复制配置模板并按需修改：

```powershell
Copy-Item config.example.toml config.toml
```

`config.toml` 含真实 API Key，**已被 `.gitignore` 排除，禁止入库**。LLM 接口遵循 OpenAI 兼容协议，支持本地模型（vLLM / Ollama）与商用中转自由切换。

关键配置项：

- `[llm] base_url / api_key / model / timeout`
- `[capture] regions / charts / feed_limit / max_benchmarks`
- `[probe] max_comments_per_app / bad_rating_threshold`
- `[audit] min_score`（默认 6.0）

## 运行

```powershell
.\run.ps1              # 交互菜单
.\run.ps1 gui          # 启动 Streamlit 情报看板
.\run.ps1 full         # 全量流水线 capture -> probe -> audit
.\run.ps1 capture      # 仅捕获
.\run.ps1 probe        # 仅探测
.\run.ps1 audit        # 仅审计
.\run.ps1 check        # 环境自检
```

也可直接使用 CLI：

```powershell
python -m radar capture --batch-id 20260101_120000
python -m radar probe   --batch-id 20260101_120000
python -m radar audit   --batch-id 20260101_120000
```

单步执行默认继承最近一次成功批次的上游产物，也可用 `--base-run-id` 指定来源批次。

## 数据与产物

所有中间产物与终产物均位于 `data/` 下，写入采用临时文件 + `os.replace` 原子替换。`data/` 已被 `.gitignore` 排除。

- `data/runs/<batch_id>/`：批次隔离目录
- `data/runs/<batch_id>/llm_traces.jsonl`：结构化 LLM 调用记录
- `data/opportunities.json`：机会清单
- `data/report.md`：决策看板

## 安全与隐私

- `config.toml`（含 API Key）与 `data/`（运行产物）均已排除入库
- 提交前请确认暂存区无密钥泄漏

## 项目结构

```
app.py                # Streamlit WebUI 情报看板
radar/
  cli.py              # CLI 入口
  config.py           # 配置加载与校验
  llm.py              # OpenAI 兼容 LLM 客户端
  store.py            # 批次归档与原子写入
  telemetry.py        # 结构化遥测事件
  pipeline/           # capture / probe / audit 三阶段
  sources/            # apple_rss / appgallery 数据源
  prompts/            # 提示词加载与内建默认模板
prompts/              # 外置提示词模板（可微调）
poc/                  # 早期接口探测与验证脚本（非正式入口）
```

## 依赖

全流程仅依赖 Python 标准库与 `httpx`、`streamlit`，不引入数据库、MQ 或复杂 ORM。