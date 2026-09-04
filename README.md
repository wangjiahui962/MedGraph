# MedGraph —— 医疗领域知识图谱构建与可视化系统

面向《中国图书馆分类法》**医药卫生（R 大类）**文本的智能体系统：自动完成
**数据采集 → 本体设计 → 数据预处理 → 信息抽取 → 知识存储 → 图谱可视化**
全流程，产出结构化三元组并存入知识库，最终通过前端图谱界面浏览、查询与展示。

## 1. 功能特性

- **R 大类语料采集（collector）**：采集 Agent 自动抓取医学百科条目
  （CLC R 医药卫生 110+ 类别），支持 MediaWiki 公开 API 与 CNKI 授权导出两种数据源，
  带断点续跑、质量门禁、原子发布，发布后自动导入 `documents.db`。
- **统一医疗本体**：9 类实体、9 类关系，含 domain/range 约束与中文标签。
- **文本预处理**：清洗 / 分句 / 分词 / 置信度标注，并统一**繁体 → 简体**
  （OpenCC t2s），保证下游词典、NER 模型、LLM 提示词口径一致。
- **分层信息抽取**（规则层 / 深度学习层 / LLM 层）：
  - **规则层**：医学词典 + 后缀模板，确定性抽取（**默认关闭**）；
  - **深度学习层**：BERT（`bert-base-chinese`）+ TokenClassification 的 NER 序列标注
    （**默认关闭**，模型在 `models/ner`）；
  - **LLM 层**：当前主抽取层，整篇文档一次调用大模型输出结构化三元组，
    输出经严格格式校验（实体/关系 ID、本体约束），格式错误自动重试、连续失败暂停。
- **SQLite 知识存储**：文档库（documents.db）与三元组库（triples.db），按事实键去重，
  三元组带 `trained` 字段记录参与模型训练的次数。
- **前端图谱可视化**：React + Cytoscape.js，支持实体/关系类型筛选、点击聚焦展开。

## 2. 系统架构

```
┌────────────┐   ┌────────────┐   ┌───────────────────────────┐
│ collector/ │   │ preprocess/│   │      extraction/           │
│  采集 Agent │──▶│  数据预处理 │──▶│ 规则层 / 深度学习层 / LLM层 │
│ MediaWiki  │   │ 清洗/繁转简 │   │（默认仅 LLM 层启用）         │
│  CNKI      │   └────────────┘   └───────────┬───────────────┘
└────────────┘                                │ 三元组
┌────────────┐   ┌────────────┐   ┌───────────▼───────────────┐
│ frontend/  │◀──│  导出 JSON  │◀──│       db/  知识存储       │
│ 图谱可视化  │   │ triples.json│   │  documents.db / triples.db│
└────────────┘   └────────────┘   └───────────────────────────┘
```

**数据流（端到端）**：

```
collector/ 采集 Agent（可自动 / 可断点续跑）
        │ 发布成功后自动导入（collector/importer.py，按 document_id 幂等 upsert）
        ▼
data/documents.db
        │  preprocess/preprocess.py       ← 清洗、分句、繁转简、置信度标注
        ▼
data/processed/preprocessed_documents.json
        │  extraction/extract.py          ← 分层抽取（默认仅 LLM 层）
        ▼
data/processed/triples_extracted.json
        │  db/store_triples.py ← 本轮结果入库；--export 导出前端 JSON
        ▼
data/triples.db  ──►  data/processed/triples.json  ──►  frontend/public/data/
```

## 3. 目录结构

```
MedGraph/
├── ontology/            # 第一阶段：本体设计
│   └── schema.py        #   9 实体 / 9 关系定义 + 校验接口，导出 data/ontology.json
├── collector/           # 数据采集 Agent（自动收集数据）
│   ├── agent.py         #   采集状态机：采集→清洗→去重→门禁→发布，支持断点续跑
│   ├── cli.py           #   CLI：plan / run / status / audit / catalog / source-check
│   ├── importer.py      #   发布后自动把文档导入 MedGraph/data/documents.db
│   ├── storage.py       #   SQLite 检查点 + 跨平台文件锁（断点续跑）
│   ├── publisher.py     #   质量门禁通过后原子发布（current 指针切换）
│   ├── normalize.py     #   规范文档 schema（documents.jsonl）
│   ├── sources/         #   数据源适配器：mediawiki.py / cnki.py
│   └── configs/         #   collection.json 配置 + clc_r_categories.csv（CLC R 类目表）
├── crawler/             # 早期的简单采集脚本（collect_sample_data.py）
├── preprocess/          # 第二阶段：数据预处理
│   └── preprocess.py    #   文本清洗 / 繁转简(OpenCC) / 分句 / 置信度标注
├── extraction/          # 第三阶段：分层信息抽取（核心）
│   ├── extract.py           # 编排入口：rule → deep_learning → llm，两遍去重合并
│   ├── rule_layer.py        # 规则层：医学词典 + 后缀模板（置信度 0.9/0.7，默认关闭）
│   ├── deep_learning_layer.py  # 深度学习层：BERT NER 推理（默认关闭）
│   ├── dl_train.py          # NER 训练脚本（--data/--epochs/--resume）
│   ├── dl_eval.py           # 验证集评估脚本（网格搜索过滤阈值）
│   ├── llm_layer.py         # LLM 层：整篇文档一次调用 + 严格格式校验（当前主抽取层）
│   └── test_extract_db.py   # 抽取 → 入库集成测试
├── db/                  # 知识存储
│   ├── init_db.py       #   建表（documents / triples，含 trained 字段自动迁移）
│   ├── store_documents.py
│   └── store_triples.py #   import 入库（增量合并）/ --export 导出前端 JSON
├── data/                # 数据与产物（外部语料/中间产物/DB 均被 .gitignore 忽略）
│   ├── raw/             #   原始采集文本；opencmkg/（外部三元组，备用）
│   ├── external/cmeee/  #   CMeEE 医学 NER 语料（download_cmeee.py 下载）
│   ├── processed/       #   预处理产物、BIO 标注、抽取结果
│   ├── documents.db     #   文档库
│   └── triples.db       #   三元组库
├── frontend/            # 第四阶段：知识展示
│   └── src/             #   React + Cytoscape 图谱可视化
├── tools/docgen/        # 文档生成工具
└── test/                # 测试
```

## 4. 本体设计（9 实体 / 9 关系）

实体类型（实体 ID / 中文标签）：

| 实体 | 标签 | 实体 | 标签 |
|------|------|------|------|
| Disease | 疾病 | Department | 科室 |
| Symptom | 症状 | Population | 人群 |
| Drug | 药物 | RiskFactor | 危险因素 |
| Treatment | 治疗方法 | Complication | 并发症 |
| Examination | 检查方法 | | |

关系类型（关系 ID / 标签 / 类型约束）：

| 关系 | 标签 | 约束（domain → range） |
|------|------|------------------------|
| HAS_SYMPTOM | 常见症状 | Disease → Symptom |
| TREATED_BY | 治疗 | Disease → Drug / Treatment |
| DIAGNOSED_BY | 检查方法 | Disease → Examination |
| HAS_RISK_FACTOR | 病因 | Disease → RiskFactor |
| HAS_SIDE_EFFECT | 不良反应 | Drug → Symptom |
| BELONGS_TO | 所属科室 | Disease → Department |
| HIGH_RISK_FOR | 高危人群 | Population → Disease |
| MAY_CAUSE | 可致并发症 | Disease → Complication |
| RELATED_TO | 相关 | 任意 → 任意（对称兜底） |

> 运行 `python ontology/schema.py` 可打印本体摘要并冻结导出 `data/ontology.json`。

## 5. 环境要求与安装

- **Python ≥ 3.11**
- **Node ≥ 22.12**（前端）

后端依赖（除深度学习层外均只用标准库）：

```powershell
# 深度学习层（NER 训练与推理）必需；已检测到 CUDA 会自动用 GPU
pip install torch transformers

# 预处理繁转简（可选，建议安装；未安装时保留繁体原样）
pip install opencc-python-reimplemented

# 预处理中文分词（可选，建议安装；缺失时退化为朴素词块切分）
pip install jieba
```

前端依赖：

```powershell
cd frontend
npm install
```

> 说明：项目未提供 `requirements.txt`，因除 `torch`/`transformers`/`opencc`/`jieba`
> 等可选依赖外全部使用 Python 标准库（urllib/sqlite3/json 等），无需额外安装。

## 6. 配置（.env）

LLM 层需要环境变量（`.env` 文件，已被 `.gitignore` 忽略，不入库）：

```ini
LLM_API_BASE=https://api.deepseek.com/v1/chat/completions
LLM_API_KEY=your_api_key
LLM_MODEL=deepseek-v4-flash
```

- `LLM_API_BASE` 填到 `/v1` 或 `/v1/chat/completions` 均可（代码自动兼容）。
- 未配置时 LLM 层不可用（`is_available()` 返回 False，抽取返回空列表），不中断流水线。
- `EXTRACT_WORKERS`（可选）：`extraction/extract.py` 的默认并发线程数，缺省 `8`，也可每次用 `--workers` 覆盖。

## 7. 快速开始（端到端）

```powershell
# ① 初始化数据库（建表）
python db/init_db.py

# ② 文档入库（raw/medical_sample.json → documents.db）
python db/store_documents.py
#    或使用采集 Agent 自动采集并发布（发布后自动导入 documents.db，见 §8）
#    从维基搜索新增几篇（繁体转简体入库）：python -m collector.wiki_add --count 5
#    （完整批量采集仍可用采集 Agent：python -m collector collect run，但门禁较严、耗时长）

# ③ 数据预处理（清洗/繁转简/分句 → preprocessed_documents.json）
python preprocess/preprocess.py

# ④ 信息抽取（默认仅 LLM 层 → triples_extracted.json）
#    默认增量：只抽新增/内容变化文档（documents.db.extract_state 记录进度，
#    首次运行会按 triples.db 已有来源自动回填，避免重复调用 LLM）
python extraction/extract.py                # 增量抽取
python extraction/extract.py --force        # 强制全量重抽（改了提示词/规则后用）
python extraction/extract.py --limit 10 --workers 4   # 前 10 篇，指定并发（默认并发=EXTRACT_WORKERS=8）

# ⑤ 三元组入库并导出前端数据（triples.db + triples.json）
python db/store_triples.py               # 本轮抽取结果入库（triples_extracted.json → triples.db）
python db/store_triples.py --export      # 从 triples.db 导出前端 data/processed/triples.json
#    （如需前端“展开原文”功能，再运行：python db/store_documents.py --export-frontend）

# ⑥ 启动前端可视化
cd frontend && npm run dev     # 打开 http://127.0.0.1:5173/
```

前端 `npm run dev` 前的 `predev` 钩子会自动把 `data/processed/triples.json`
同步到 `frontend/public/data/triples.json`，并同步 `documents.json`（证据面板“展开”
查看文档原文用，由 `python db/store_documents.py --export-frontend` 生成），无需手动复制。

## 8. 数据采集 Agent（collector）

采集 Agent 面向 CLC R 大类（医药卫生，110+ 类别）自动收集医学文本，并直接喂给下游
预处理 / 抽取流水线。相比早期 `crawler/` 的简单脚本，它具备完整的生产级生命周期：

- **状态机**：CREATED → PLANNING → ACQUIRING → NORMALIZING → DEDUPING →
  VALIDATING → PUBLISHING，全程 SQLite 检查点记录进度；
- **断点续跑**：中断后 `--resume` 即可从上次检查点继续，不会重复采集；
- **质量门禁**：发布前强制校验最少文档数 / 最少类别覆盖 / 每类别最低条数
  （配置在 `collector/configs/collection.json` 的 `quality_gates`）；
- **原子发布**：门禁通过后才切换 `current` 指针，历史版本按 run_id 归档；
- **自动入库**：发布成功后由 `collector/importer.py` 把规范 `documents.jsonl`
  转成九字段记录，按 `document_id` 幂等 upsert 进 `MedGraph/data/documents.db`。

数据源（`--source`）：

| 数据源 | 说明 | 前置条件 |
|--------|------|----------|
| `mediawiki` | 维基百科公开 API（默认） | 可访问外网（zh.wikipedia.org 境内常需代理） |
| `cnki` | 知网授权网络内合法导出 | 用户手工导出的题录/摘要目录（`--input-dir`） |

常用命令：

```powershell
# 查看命令帮助
python -m collector --help

# ① 校验 CLC R 类目表（要求启用且已审核 ≥100 类）
python -m collector catalog

# ② 检查数据源前置条件（如 MediaWiki 可达性 / CNKI 导出目录）
python -m collector source-check --source mediawiki

# ③ 生成逐类别采集计划（collection_plan.csv）
python -m collector collect plan

# ④ 运行采集 Agent（采集→清洗→去重→门禁→发布→自动导入 documents.db）
python -m collector collect run --run-id run_20260903 --workers 4

# ⑤ 断点续跑（中断后恢复）
python -m collector collect run --resume run_20260903

# ⑥ 查看 run 状态 / 校验当前发布版本
python -m collector collect status run_20260903
python -m collector audit
```

## 9. 分层信息抽取详解

三层结构由 `extraction/extract.py` 编排。当前**规则层与深度学习层默认关闭**，
实际抽取以 **LLM 层**为主（每篇整篇文档一次调用）；需要时可按 §9.1 / §9.2 打开。

### 9.1 规则层（rule_layer.py，默认关闭）

- **词典命中**（置信度 0.9）：医学词典精确匹配，边界准确；
- **后缀模板**（置信度 0.7）：按"X 的治疗方法/症状/检查"等模板切宾语，
  存在吞字/错切风险；
- 关闭原因：把"标题当疾病主语"（概念页/事件页标题如"带原者"也被当疾病）且模板吞字
  噪声大、与深度学习层结果大量重复，信息抽取不再使用。
- 恢复方法：把 [rule_layer.py](extraction/rule_layer.py) 顶部的 `AVAILABLE` 改回 `True`。
  注意：其医学词典仍被深度学习层做词面校验、被 `dl_train` 做弱监督数据复用，不受开关影响。

### 9.2 深度学习层（deep_learning_layer.py，默认关闭）

- 建模为**中文序列标注（BIO NER）**：`O / B·I-Disease / B·I-Symptom / B·I-Drug /
  B·I-Treatment / B·I-Examination / B·I-RiskFactor`；
- 推理后处理：单字过滤（`MIN_ENTITY_LEN=2`）、剥尾部连接词、医学词典词面校验、
  Symptom 黑名单（死亡/发病/疫情）、纯英文词过滤、自环（subject==object）过滤、
  主语-客体同子句关联约束；
- 关闭原因：当前阶段改用 LLM 完成信息抽取，本层不再产出三元组；
  模型权重仍在 `models/ner`，训练/评估脚本可独立运行。
- 恢复方法：把 [deep_learning_layer.py](extraction/deep_learning_layer.py) 顶部的
  `DEEP_LEARNING_ENABLED` 改回 `True`。

### 9.3 LLM 层（llm_layer.py，当前主抽取层）

- **整篇文档一次调用**：把「标题 + 全文」一次性交给 LLM（不做逐句路由），
  模型看到完整上下文抽取更准，每篇仅 1 次网络调用；
- **严格输出校验**：要求返回 JSON 数组，校验 JSON 可解析、必填字段齐全、
  实体/关系 ID 合法、满足本体 domain → range 约束，非法项剔除；
- **快速模式（默认开启）**：请求带 `thinking={"type": "disabled"}` 关闭模型思考/推理链
  （deepseek 系列默认会先做长思考，单篇白白多烧数秒），结构化抽取直接输出结果；
- **自动重试**：网络/HTTP 异常（单次超时 60s、5xx、429 限流）按抖动退避自动重试，429 优先
  遵循服务端 `Retry-After`（总尝试次数 `RETRY_MAX_ATTEMPTS=3`）；输出格式错误会把错误信息
  回传模型重新生成（最多 `MAX_LLM_RETRIES=3` 次）。重试耗尽仍失败抛 `RuntimeError`，
  由 `extract.py` **报错暂停**并落盘已抽结果（避免在故障/坏格式上反复消耗 token/费用）；
- **并发加速**：`extraction/extract.py` 默认 8 线程并发调用 LLM（纯网络 IO，可安全并发），
  可用环境变量 `EXTRACT_WORKERS` 调整、`--workers` 参数优先；未配置 `.env` 时本层整篇
  降级为空结果，不中断流水线；
- 能补齐仅用 CMeEE 训练的 NER 模型学不到的关系（`HAS_SIDE_EFFECT` /
  `MAY_CAUSE` / `RELATED_TO` / `HAS_RISK_FACTOR` 等）。

### 9.4 分层结果合并（extract.py）

两遍去重：

1. **精确去重**：按 `(subject, relation, object, object_type)` 去重，保留置信度更高者
   （置信度相同时优先规则层结果）；
2. **跨层语义去重**：DL 与 LLM 常对同一对实体给出不同关系（如 DL 判 HAS_SYMPTOM、
   LLM 判 MAY_CAUSE），此时按关系"具体度" `_RELATION_SPECIFICITY` 保留最具体的一条
   （泛化的 `RELATED_TO` 优先级最低），并列时按置信度、其次按层（deep_learning > llm），
   保证同一对实体只保留一条事实。

## 10. 深度学习层训练

> 训练/评估独立于 §9.2 的抽取开关，可随时运行（产出写入 `models/ner`）。

### 10.1 训练数据来源

| 来源 | 说明 | 脚本 |
|------|------|------|
| **CMeEE** | CBLUE 医学 NER 金标语料（9 类人工标注，映射到项目 6 类 NER） | `data/download_cmeee.py` → `data/convert_cmeee.py` |
| 弱监督 | 规则层高置信词典命中自动转 BIO（补充 RiskFactor） | `dl_train.py` 的 `build_dataset()` |
| 远程监督 | CMeEE 实体大词典匹配项目文档 | `data/build_distant_labels.py` |

### 10.2 训练命令

```powershell
# 从头训练（默认合并集，3 轮）
python extraction/dl_train.py

# 仅用 CMeEE，训练 1 轮
python extraction/dl_train.py --data data/processed/ner_cmeee_labels.json --epochs 1

# 在已有权重上继续增量训练（小学习率 1e-5）
python extraction/dl_train.py --data data/processed/ner_cmeee_labels.json --epochs 2 --resume
```

- 模型保存到 `models/ner`，推理层自动懒加载；
- 已检测到 GPU（如 RTX 3060）时自动用 CUDA 训练。
- 训练完成后可把 `triples.db` 中相关三元组的 `trained` 字段按 `source_ids` 关联 +1，
  记录该三元组参与模型训练的次数。

### 10.3 阈值调优评估

```powershell
python extraction/dl_eval.py    # 在验证集上网格搜索长度/置信度阈值
```

## 11. 前端可视化

技术栈：React 19 + TypeScript + Vite + Cytoscape.js（fcose 力导向布局）。

```powershell
cd frontend
npm install
npm run dev        # http://127.0.0.1:5173/
npm run build      # 类型检查 + 生产构建
npm test           # vitest 单测
```

功能：
- 图谱画布（楷体标签、节点随关联度缩放、fcose 力导向布局）；
- 按实体类型 / 关系类型筛选；
- 点击节点聚焦展开其一跳邻居；
- 右侧面板展示实体/关系详情。

## 12. 数据库说明

- `data/documents.db` —— 文档库，表 `documents`
  （document_id / category_ids / title / content / source_url / license /
  collected_at / content_hash / quality_score）。
- `data/triples.db` —— 三元组库，表 `triples`：

| 字段 | 说明 |
|------|------|
| subject / subject_type | 主语实体及其类型 |
| relation / object / object_type | 关系及客体实体 |
| source_document_id / source_text | 来源文档与原文证据句 |
| confidence | 置信度（0~1） |
| layer | 产出层：rule / deep_learning / llm |
| trained | 参与模型训练的次数（每训练一次 +1，默认 0） |

- 去重策略：`(subject, relation, object, object_type)` 唯一索引（`INSERT OR IGNORE`）。
- 旧库自动迁移：`db/init_db.py` 检测 `trained` 列缺失时用 `ALTER TABLE` 补上（幂等）。

## 13. 数据来源与版权

- **采集 Agent（collector）**：MediaWiki 公开 API / CNKI 授权导出，均须遵守来源页面的
  许可与署名要求（`collector/configs/collection.json` 中配置了 rights_statement）。
- **CMeEE**（CBLUE 中文医学信息抽取基准）—— 仅用于模型训练（下载见 `data/download_cmeee.py`）。
- **OpenCMKG**（`data/raw/opencmkg/`）—— 外部中文医学三元组（35 万+ 条），
  当前备用未接入训练，不入库。
- 项目文档语料来自公开百科条目，仅供教学/科研用途。

## 14. 已知限制与改进方向

- **外网可达性**：MediaWiki（zh.wikipedia.org）在境内通常无法直连，大规模采集
  需配置代理或改用 CNKI 本地导出数据源；
- **LLM 成本与速度**：LLM 调用是主瓶颈（实测单篇 3~150s、长尾明显，输入仅约 1000 token），`extract.py` 默认 8 线程并发
  （`--workers N` / `EXTRACT_WORKERS` 可调）、单次超时 60s、429 按 `Retry-After` 抖动退避；
  已默认关闭模型思考链（`thinking=disabled`），单篇稳定在数秒内；
  `deepseek-v4-flash` 等低价模型可控制成本；
  `deepseek-v4-flash` 等低价模型可控制成本；
- **RiskFactor 学不到**（深度学习层）：CMeEE 无此类型，需靠弱监督 / LLM 标注补充训练数据；
- **领域漂移**：CMeEE 为临床文本风格，与百科条目有差异，可加入项目语料远程监督
  或 LLM 辅助标注弥合；
- **关系判定**：深度学习层为"NER + 启发式关联"，未做显式关系分类，
  长尾/跨句关系依赖 LLM 层补全。

## 15. 常见问题

- **怎么只用 LLM 抽取？** 默认即如此：规则层与深度学习层均为 `False` 开关关闭，
  `extract.py` 只跑 LLM 层。运行前确认 `.env` 已配置 LLM_API_BASE / LLM_API_KEY / LLM_MODEL。
- **LLM 层报错/暂停？** 连续 `MAX_LLM_RETRIES=3` 次输出格式错误、或网络/HTTP 异常重试耗尽，
  会抛 `RuntimeError` 报错暂停（避免静默丢数据/消耗费用）；请检查提示词/模型/网络后重试，
  已抽部分结果会落盘到 `triples_extracted.json`。仅当未配置 `.env` 时本层才降级为空结果。
- **怎么恢复规则层/深度学习层？** 分别把 `rule_layer.py` 的 `AVAILABLE`、
  `deep_learning_layer.py` 的 `DEEP_LEARNING_ENABLED` 改回 `True`。
- **采集卡在 healthcheck？** 确认 `.env`/外网可达；MediaWiki 要求 UA 不含 "contact"
  且为真实项目标识（见 `collector/configs/collection.json`）。
- **前端空白？** 确认已运行 `python db/store_triples.py --export` 生成
  `triples.json`，且 `npm run dev`（predev 会自动同步数据）。
- **大文件不入库**：`models/`、`data/external/`、`data/processed/`、
  `collector/data/`、`data/raw/opencmkg/`、`*.db`、`.env` 均在 `.gitignore` 中，
  提交代码时自动排除。
