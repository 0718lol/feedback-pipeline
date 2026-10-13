# 多平台用户反馈采集与智能分析系统 (Multi-Platform User Feedback Pipeline)

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Tests: passing](https://img.shields.io/badge/tests-10%2F10%20passed-brightgreen.svg)](#单元测试与质量验证)

一个面向生产级可靠性设计、支持多平台（Discord 官方 API / 小红书公开页 Playwright）的用户反馈数据流水线。实现 **『数据采集 → 清洗校验 → 增量同步 → 去重幂等 → 结构化存储 → LLM 智能分析 → 全链路可追溯』** 的闭环工程体系。

---

## 目录
- [一、项目架构与设计](#一项目架构与设计)
- [二、核心工程特性实现](#二核心工程特性实现)
- [三、快速开始（5 步跑通）](#三快速开始5-步跑通)
- [四、CLI 常用命令](#四cli-常用命令)
- [五、单元测试与质量验证](#五单元测试与质量验证)
- [六、工程设计与技术取舍说明（面试核心）](#六工程设计与技术取舍说明面试核心)
- [七、目录结构](#七目录结构)

---

## 一、项目架构与设计

```
[数据源层]
  ├── Discord (官方 Bot REST API v10 / 游标 incremental query)
  └── 小红书 (Playwright 动态无头浏览器 / storage_state 登录态复用)
         │
         ▼
[流控与安全层]
  ├── 随机抖动延时 (2.0s ~ 5.0s Jitter)
  ├── 指数退避重试 (Exponential Backoff, 3次上限)
  └── 异常隔离机制 (Fault Isolation: 单源故障不阻断全局任务)
         │
         ▼
[清洗与契约层]
  └── 统一数据契约 FeedbackItem Schema (严格校验：ID、时间戳、作者、互动数、溯源URL)
         │
         ▼
[存储与状态层 (SQLite WAL)]
  ├── feedbacks (唯一约束保证去重与幂等写入)
  ├── checkpoints (水位线游标持久化，支持中断后断点续传)
  └── collection_runs (运行遥测日志与质量监控报警)
         │
         ▼
[LLM 智能分析引擎 (OpenAI Compatible)]
  ├── 情感分类 (positive / negative / neutral)
  ├── 问题聚类 (bug / feature_request / inquiry / praise / other)
  └── 结构化结果关联入库 (analysis_results 表外键约束，端到端可追溯)
```

---

## 二、核心工程特性实现

| 特性 | 工程实现机制 | 解决的痛点 |
|---|---|---|
| **去重幂等** | 数据库以平台前缀 ID（如 `discord:{channel}:{msg_id}`）为 Primary Key，底层执行 `INSERT OR IGNORE` | 重复调度或异常重跑不会产生任何重复脏数据 |
| **增量同步** | 基于 Watermark 机制。Discord 采用 `after={last_item_id}` 游标；网页采集比对已入库的水位线 | 避免重复抓取历史旧数据，节约 90%+ 的网络请求与算力 |
| **断点续传** | `checkpoints` 表实时持久化每个 Channel/Keyword 的采集断点，崩溃重启自动从断点拉取 | 避免网络抖动或手动中断导致任务必须重头开始 |
| **登录态持久化** | Playwright `storage_state` 将 Cookie 与 Session 序列化至本地 JSON 文件持久化复用 | 杜绝每次运行弹出登录页，降低账号风控风险 |
| **全链路溯源** | 统一数据契约强制要求 `url` 字段，每条 LLM 输出均能通过反馈 ID 联动查看原帖 URL | 确保分析结论经得起真实业务排查与还原 |
| **监控与告警** | 捕获空采（Count=0）及字段缺失率（>20%）并输出告警，遥测数据记录至 `collection_runs` | 第一时间发现反爬升级、页面改版或 Token 失效 |
| **异常隔离** | Pipeline 调度器对各采集器进行上下文隔离，平台 A 异常不影响平台 B | 确保分布式多源系统的健壮性 |

---

## 三、快速开始（5 步跑通）

### 第 1 步：克隆仓库并进入目录
```bash
git clone https://github.com/your-username/feedback-pipeline.git
cd feedback-pipeline
```

### 第 2 步：创建并激活 Python 虚拟环境
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 第 3 步：配置环境变量
```bash
cp .env.example .env
# 编辑 .env 文件，填入你的 API 配置（未配置 Token 时系统将自动安全切换至内置仿真模式）
```

### 第 4 步：安装依赖（按需安装自动化浏览器）
```bash
pip install -r requirements.txt
# 若需要运行小红书真实网页采集，安装 Playwright 浏览器内核：
# playwright install chromium
```

### 第 5 步：一键运行全链路流水线
```bash
python3 main.py run
```

---

## 四、CLI 常用命令

| 命令 | 说明 | 示例 |
|---|---|---|
| `python3 main.py run` | 运行完整链路（全平台采集 → 增量入库 → LLM 分析 → 打印报告） | `python3 main.py run` |
| `python3 main.py collect` | 仅执行采集模块（可指定 `--platform`） | `python3 main.py collect --platform discord` |
| `python3 main.py analyze` | 仅对数据库中未分析的反馈运行 LLM 结构化提取 | `python3 main.py analyze --batch-size 50` |
| `python3 main.py stats` | 输出系统级遥测健康报告与情感/分类分布 | `python3 main.py stats` |
| `python3 main.py trace <id>` | 审计溯源：通过 ID 查询该条分析并返回原帖真实 URL | `python3 main.py trace discord:mock_channel_feedback:120000000000000001` |
| `python3 main.py test` | 一键执行测试套件 | `python3 main.py test` |

---

## 五、单元测试与质量验证

系统自带轻量且严谨的单元测试套件，零外部网络依赖，测试覆盖核心工程逻辑：

```bash
python3 main.py test
```

### 测试用例覆盖明细：
- `test_idempotency.py`: 连续插入相同数据 3 次，断言数据库记录数始终为 1。
- `test_checkpoint.py`: 模拟程序中途崩溃退出，验证重启后正确读取断点水位。
- `test_schema.py`: 验证统一数据契约校验规则（必填字段、非法 URL 拦截）。
- `test_fault_isolation.py`: 故意模拟 Discord 平台抛出网络异常，验证小红书采集任务照常成功完成。
- `test_llm_parser.py`: 验证 Markdown 代码块清洗、JSON 提取容错与启发式离线降级引擎。
- `test_traceability.py`: 抽取数据验证每条分析结果均可正确映射回真实原帖 URL。

---

## 六、工程设计与技术取舍说明（面试核心）

在面试中被问及系统架构与技术选型时，可重点阐述以下四点设计权衡：

### 1. 为什么采用 SQLite WAL 模式而不是直接上 MySQL / PostgreSQL？
- **设计权衡**：本项目定位于单机可独立运行、轻量易交付的采集与分析系统。SQLite 零配置、无守护进程成本，适合嵌入式部署。
- **并发保障**：通过启用 `PRAGMA journal_mode = WAL`（预写日志），实现**读写互不阻塞**，并开启 `foreign_keys = ON` 维持严格的外键参照完整性，满足每秒数千次轻量写入的性能要求。

### 2. 增量同步为什么采用游标（Watermark）而非全量扫描比对？
- **设计权衡**：若每次抓取全量数据再入库比对去重，随着历史数据规模扩大，网络 I/O 开销与计算复杂度呈 $O(N)$ 激增，极易触发平台反爬。
- **解决方案**：针对 Discord 官方 API 利用其原生 `after={msg_id}` 游标，在接口层仅请求增量数据；小红书端在滑动列表遇到小于等于 Watermark 的 Note ID 时立即短路终止，将网络请求降至最低。

### 3. 限流与反爬对抗：为什么采用随机抖动延时（Jitter）与指数退避？
- **设计权衡**：固定周期的请求容易被平台的流量行为分析特征识别，而纯并发抓取容易引发 HTTP 429。
- **解决方案**：每次请求引入 2.0s ~ 5.0s 的伪随机离散延时，并在遭遇网络抖动或 429 限流时，触发初始为 2s、倍数为 2 的指数退避重试（带微抖动），最大重试 3 次，平衡采集吞吐率与合规安全性。

### 4. 为什么在 LLM 模块中设计离线规则降级与清洗容错？
- **设计权衡**：外部大模型 API 存在网络不稳定、Token 额度耗尽以及响应格式夹杂 Markdown 标记的实际痛点。
- **解决方案**：
  1. 通过正则解析与括号匹配消除 ```` ```json ```` 等格式干扰；
  2. 实现本地启发式分类引擎作为 Fallback，保证在断网或无 Key 状态下，系统仍具备完整的冒烟测试和离线演示能力。

---

## 七、目录结构

```
feedback-pipeline/
├── .env.example              # 环境变量配置模板
├── .gitignore                 # 敏感文件与缓存过滤规则
├── README.md                  # 项目工程文档与面试指南
├── requirements.txt           # 依赖清单
├── main.py                    # 命令行调度入口
├── data/                      # 本地 SQLite 数据库与登录态存储
│   └── feedbacks.db
├── logs/                      # 运行日志
│   └── pipeline.log
├── src/
│   ├── analyzer/              # LLM 结构化提取模块
│   │   └── llm_analyzer.py
│   ├── collectors/            # 多平台采集器实现
│   │   ├── base.py            # 抽象基类（含遥测、契约验证与监控报警）
│   │   ├── discord_collector.py
│   │   └── xhs_collector.py
│   ├── models/                # 统一契约与数据库模型
│   │   ├── db.py              # SQLite 连接池与建表语句
│   │   └── schema.py          # FeedbackItem / AnalysisResult 数据契约
│   ├── storage/               # 仓储与断点管理
│   │   ├── checkpoint.py      # Watermark 增量水位管理
│   │   └── repository.py      # 数据访问层（去重幂等与溯源查询）
│   ├── utils/                 # 工具类
│   │   ├── config.py          # 零依赖配置解析
│   │   ├── logger.py          # 结构化双写日志
│   │   └── retry.py           # 指数退避与抖动装饰器
│   └── pipeline.py            # 主调度控制器（异常隔离）
└── tests/                     # 单元测试套件
    ├── test_checkpoint.py
    ├── test_fault_isolation.py
    ├── test_idempotency.py
    ├── test_llm_parser.py
    ├── test_schema.py
    └── test_traceability.py
```
