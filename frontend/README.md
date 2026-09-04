# MedGraph 知识图谱前端

独立的 React + TypeScript + Vite + Cytoscape.js 中文工作台，只读取现有三元组文件。无需数据库、后端、密钥或外部 CDN。不包含采集、抽取、智能体或 PPT。

## 启动

要求 Node.js 22.12+（已在 Node 24 上验证）。在 PowerShell 中运行：

```powershell
cd 'D:\learn_nk\agent\homework\实训小组作业\frontend'
npm ci
npm run dev
```

打开终端显示的本机地址，通常是 http://127.0.0.1:5173/ 。按 Ctrl+C 停止服务。不要直接双击 HTML 文件。

```powershell
npm test
npm run build
npm run preview
```

`build` 包含 TypeScript 检查，输出 `dist/`。`preview` 通常使用 4173 端口。本工程不自动发布到互联网。

> 顶部“增加新数据 / 提取现有数据”依赖 `python server.py` 提供的 `/api`，只有 `npm run dev` 开发模式会代理 `/api`；`npm run preview` 或构建产物没有该代理，这几个按钮会提示无法连接后端。
>
> “增加新数据”走后端 `/api/collect`：从中文维基百科搜索新增文章（顶部可自定义输入篇数，1~100 的任意整数），抓取正文后经 OpenCC
> 繁体转简体存入 documents.db，此步骤不做 LLM 抽取；随后点“提取现有数据”才会执行 LLM 抽取 → 关键词过滤 → 入库 → 导出前端。

## 数据与统计口径

`predev` 和 `prebuild` 会自动将 `../data/processed/triples.json` 复制到 `public/data/triples.json`，并把 `../data/processed/documents.json` 复制到 `public/data/documents.json`，不修改源文件。`documents.json` 由 `python db/store_documents.py --export-frontend` 从文档库生成，供证据面板“展开”显示文档原文；正文/标题在导出时会经 OpenCC t2s 繁体转简体，与抽取原句同一简体口径。生成副本与构建产物不提交。更新数据后重启开发服务或重新构建；只运行 preview 不会重新同步。

数据必须是 JSON 数组，每条记录字段为：

```json
{
  "subject": "实体甲",
  "subject_type": "疾病",
  "relation": "关联",
  "object": "实体乙",
  "object_type": "症状",
  "source_document_id": "doc_示例",
  "source_text": "来源原句"
}
```

上述仅为字段格式示意，不属于实际医学知识。前端只加载项目真实文件。

`documents.json` 每条记录字段为：

```json
{
  "document_id": "doc_示例",
  "title": "文档标题",
  "content": "文档全文原文"
}
```

- 实体键为名称与类型的组合；同名不同类型保留为不同节点。
- 关系按两端实体与关系名称去重；来源按文档编号与原句组合去重。
- 三元组记录数是数组总长度，包含被跳过的非法记录；实体、关系与来源文档统计只来自有效记录。
- 来源文档数是有效记录中非空文档编号的去重数，不是采集文本总数。原始文本类别数量也不是实体类型数。
- 缺少有效主语、关系或宾语的记录被跳过并提示；缺失类型标为“未分类”。缺失证据显示相应空状态。
- 保留原始抽取类型及原句，不在展示层纠正数据。现有抽取脚本已在写文件时去重，前端不能恢复源文件未保存的证据。

## 演示步骤

1. 查看顶部四项统计。默认中心为关联度最高的实体。
2. 搜索“猪瘟”，选择结果，查看一跳关系。
3. 点击“常见症状”的“查看证据”，检查来源编号和原句。
4. 返回详情，点击“腹泻”继续探索其关联实体。
5. 切换实体类型或关系类型筛选；使用放大、缩小、适应画布和恢复默认。

筛选要求连线的两个端点类型均被选中；没有满足关系条件的孤立实体不出现在搜索目录中。搜索只过滤目录，选中结果才改变图谱中心。中心被筛选排除时自动选择当前结果中关联度最高者。目录每次显示 40 条，可以加载更多。

默认与查询视图最多 80 个节点；先保留中心，其余按关联度降序、名称稳定排序。详情列表仍包含该中心在当前筛选下的所有关联关系。节点点击改变中心，连线点击查看证据。画布操作也可通过搜索按钮与关系列表完成键盘替代操作。

## 自动化浏览器验收

```powershell
npx playwright install chromium
npx playwright test
```

测试会启动本地开发服务或复用 5173 端口现有服务。也可使用已安装的 Edge：

```powershell
$env:PLAYWRIGHT_CHANNEL = 'msedge'
npx playwright test
```

浏览器测试覆盖真实数据查询、证据、切换邻居、筛选和重置；模拟网络错误、空数据、非法记录、截断大图和 HTML 文本；检查窄屏及长名称。真实数据测试以仓库当前的 857 条记录和“猪瘟”样例为基线，换数据后需同步调整断言。

`src/graph.ts` 是数据适配与查询入口；以后对接后端可替换加载函数，保持组件接收的图结构不变。本次没有实现任何后端 API。

自动抽取结果仅用于课程展示，不作为医疗建议。证据面板默认只显示抽取原句，点击“展开”才按需读取 `documents.json` 展示对应文档原文；前端不虚构实体简介或来源链接。
