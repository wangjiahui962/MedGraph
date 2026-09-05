import { useEffect, useMemo, useRef, useState } from "react";
import {
  filterGraph,
  fullGraph,
  confidenceView,
  bipartiteView,
  similarityView,
  loadGraph,
  neighborhood,
  searchNodes,
  overview,
  type GraphMode,
  type Graph,
  type Relation,
} from "./graph";
import {
  GraphCanvas,
  colorForType,
  shapeForType,
} from "./GraphCanvas";
import { clearDocumentsCache, fetchDocument, type DocumentRecord } from "./documents";
import {
  fetchJobs,
  startCollect,
  startExtract,
  type Job,
} from "./api";

const TYPE_LABELS: Record<string, string> = {
  Disease: "疾病",
  Drug: "药物",
  Symptom: "症状",
  Treatment: "治疗方法",
  Examination: "检查方法",
  Complication: "并发症",
  RiskFactor: "危险因素",
  Department: "科室",
  Population: "人群",
};

const RELATION_LABELS: Record<string, string> = {
  HAS_SYMPTOM: "常见症状",
  TREATED_BY: "治疗方法",
  DIAGNOSED_BY: "检查方法",
  MAY_CAUSE: "病因 / 可能导致",
  HAS_RISK_FACTOR: "危险因素",
  HAS_SIDE_EFFECT: "不良反应",
  HIGH_RISK_FOR: "高风险人群",
  BELONGS_TO: "所属分类",
  RELATED_TO: "相关关系",
  SHARES_SYMPTOM: "共享症状",
};

const MODE_META: Record<GraphMode, { label: string; chip: string; description: string }> = {
  overview: {
    label: "结构概览",
    chip: "快速概览",
    description: "展示高连接度代表性结构；搜索与详情仍覆盖完整数据",
  },
  focus: {
    label: "实体聚焦",
    chip: "一跳邻域",
    description: "围绕一个实体阅读它的一跳关系和原文证据",
  },
  confidence: {
    label: "可信筛选",
    chip: "阈值可调",
    description: "按关系置信度筛选；阈值越高，网络通常越稀疏",
  },
  bipartite: {
    label: "疾病—症状",
    chip: "二部图",
    description: "疾病与症状分列两侧，共享症状自然汇聚",
  },
  similarity: {
    label: "相似疾病",
    chip: "派生视图",
    description: "根据共享症状连接疾病，线越粗表示共享症状越多",
  },
  full: {
    label: "全量图",
    chip: "完整数据",
    description: "加载全部节点和关系，使用默认力导向布局观察总体结构",
  },
};

const typeLabel = (type: string) => TYPE_LABELS[type] ?? type;
const relationLabel = (relation: string) => RELATION_LABELS[relation] ?? relation;
const confidenceLabel = (value: number) =>
  value >= 0.8 ? "高可信" : value >= 0.6 ? "中可信" : "待核验";

export default function App() {
  const [graph, setGraph] = useState<Graph | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [chosenTypes, setChosenTypes] = useState<string[]>([]);
  const [chosenRelations, setChosenRelations] = useState<string[]>([]);
  const [focus, setFocus] = useState<string | null>(null);
  const [graphMode, setGraphMode] = useState<GraphMode>("overview");
  const [confidenceThreshold, setConfidenceThreshold] = useState(0.7);
  const [legendOpen, setLegendOpen] = useState(false);
  const [edgeId, setEdgeId] = useState<string | null>(null);
  const [resultLimit, setResultLimit] = useState(40);
  const [resetKey, setResetKey] = useState(0);
  // 采集 / 抽取任务状态
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobError, setJobError] = useState("");
  const [jobBusy, setJobBusy] = useState(false);
  // “增加新数据”每次从维基新增的篇数（可自定义输入，1~100）
  const [wikiCount, setWikiCount] = useState("5");
  const collectCount = Math.min(
    100,
    Math.max(1, Math.round(Number(wikiCount) || 5)),
  );
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // 记录“曾处于 running”的任务 id：数据任务成功结束时触发图谱自动刷新
  const runningSeenRef = useRef<Set<string>>(new Set());

  const refreshJobs = async (silent = false) => {
    try {
      const { jobs: list } = await fetchJobs();
      setJobs(list);
      setJobError("");
    } catch (e) {
      if (!silent)
        setJobError(
          e instanceof Error ? e.message : "无法连接本地后端服务。",
        );
      // 后端未启动时静默处理，不打扰浏览
    }
  };

  // 有任务运行中则轮询，任务全部结束后停止
  useEffect(() => {
    if (jobs.some((j) => j.status === "running")) {
      pollRef.current ??= setInterval(() => refreshJobs(true), 3000);
    } else if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [jobs]);

  // 挂载时恢复一次任务状态：刷新页面后仍能显示并轮询后端未完成的任务
  useEffect(() => {
    refreshJobs(true);
  }, []);

  const runJob = async (kind: "collect" | "extract", count = 5) => {
    setJobError("");
    setJobBusy(true);
    try {
      const started =
        kind === "extract"
          ? await startExtract()
          : await startCollect(count, true);
      runningSeenRef.current.add(started.job_id);
      // 注入一条本地 running 占位，立即轮询后端真实状态
      setJobs((prev) => [
        {
          id: started.job_id,
          kind,
          status: "running",
          started_at: "刚刚",
          finished_at: null,
          message: "任务已启动…",
          detail: null,
        },
        ...prev,
      ]);
      await refreshJobs(true);
    } catch (e) {
      setJobError(
        e instanceof Error ? e.message : "任务启动失败，请检查后端服务。",
      );
    } finally {
      setJobBusy(false);
    }
  };

  // 采集（自动抽取）或“提取现有数据”成功结束 → 重新读取 triples.json
  useEffect(() => {
    const runningNow = new Set(
      jobs.filter((j) => j.status === "running").map((j) => j.id),
    );
    for (const job of jobs) {
      if (
        (job.kind === "extract" || job.kind === "collect") &&
        job.status === "succeeded" &&
        runningSeenRef.current.has(job.id)
      ) {
        runningSeenRef.current.delete(job.id);
        setAttempt((n) => n + 1);
      }
    }
    runningSeenRef.current = runningNow;
  }, [jobs]);

  const hasRunning = jobs.some((j) => j.status === "running");
  const latestJob = jobs[0];

  useEffect(() => {
    const abort = new AbortController();
    setGraph(null);
    setError("");
    clearDocumentsCache();
    loadGraph(abort.signal)
      .then((g) => {
        setGraph(g);
        setChosenTypes([...new Set(g.nodes.map((n) => n.type))]);
        setChosenRelations([...new Set(g.edges.map((e) => e.relation))]);
      })
      .catch((e) => {
        if (!abort.signal.aborted)
          setError(
            e instanceof Error
              ? e.message
              : "无法读取数据，请检查本地服务后重试。",
          );
      });
    return () => abort.abort();
  }, [attempt]);
  const types = useMemo(
    () => [...new Set(graph?.nodes.map((n) => n.type))].sort(),
    [graph],
  );
  const relations = useMemo(
    () => [...new Set(graph?.edges.map((e) => e.relation))].sort(),
    [graph],
  );
  const filtered = useMemo(
    () =>
      graph
        ? filterGraph(
            graph,
            chosenTypes,
            chosenRelations,
            graphMode === "overview" || graphMode === "full",
          )
        : null,
    [graph, chosenTypes, chosenRelations, graphMode],
  );
  const view = useMemo(() => {
    if (!filtered) return null;
    if (graphMode === "overview") return overview(filtered);
    if (graphMode === "full") return fullGraph(filtered);
    if (graphMode === "confidence")
      return confidenceView(filtered, confidenceThreshold);
    if (graphMode === "bipartite") return bipartiteView(filtered);
    if (graphMode === "similarity") return similarityView(filtered);
    // 搜索结果可能因左侧筛选被隐藏；选中后用完整图谱定位，避免出现“查到但看不到”。
    const focusVisible = !focus || filtered.nodes.some((n) => n.id === focus);
    return neighborhood(focus && !focusVisible ? graph ?? filtered : filtered, focus);
  }, [filtered, graph, focus, graphMode, confidenceThreshold]);
  const results = useMemo(
    () => (filtered ? searchNodes(filtered, query) : []),
    [filtered, query],
  );
  const selected = graph?.nodes.find((n) => n.id === view?.focusId);
  const selectedEdge =
    view?.edges.find((e) => e.id === edgeId) ??
    graph?.edges.find((e) => e.id === edgeId);
  const selectedAllEdges = graph?.edges.filter(
    (e) => e.source === selected?.id || e.target === selected?.id,
  ) ?? [];
  const relationGroups = useMemo(() => {
    const groups = new Map<string, Relation[]>();
    for (const edge of selectedAllEdges) {
      const bucket = groups.get(edge.relation) ?? [];
      bucket.push(edge);
      groups.set(edge.relation, bucket);
    }
    return [...groups.entries()].sort((a, b) => relationLabel(a[0]).localeCompare(relationLabel(b[0]), "zh-CN"));
  }, [selectedAllEdges]);
  const names = useMemo(
    () => new Map(graph?.nodes.map((n) => [n.id, n.name])),
    [graph],
  );
  function pickNode(id: string) {
    setFocus(id);
    setGraphMode("focus");
    setEdgeId(null);
  }
  function reset() {
    setQuery("");
    setChosenTypes(types);
    setChosenRelations(relations);
    setFocus(null);
    setGraphMode("overview");
    setEdgeId(null);
    setResultLimit(40);
    setConfidenceThreshold(0.7);
    setLegendOpen(false);
    setResetKey((k) => k + 1);
  }
  function toggle(
    value: string,
    current: string[],
    setter: (v: string[]) => void,
  ) {
    setter(
      current.includes(value)
        ? current.filter((v) => v !== value)
        : [...current, value],
    );
    setEdgeId(null);
    setResultLimit(40);
  }
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-symbol">
            M<span>·</span>
          </span>
          <div>
            <strong>MedGraph</strong>
            <span className="brand-sub">医学知识图谱</span>
          </div>
        </div>
        <div className="workspace-name">
          知识探索工作台 <span>EXPLORER</span>
        </div>
        <div className="top-actions">
          <div className="sample-add">
            <input
              type="number"
              min={1}
              max={100}
              step={1}
              inputMode="numeric"
              aria-label="输入从维基新增的篇数"
              value={wikiCount}
              onChange={(e) => setWikiCount(e.target.value)}
              disabled={jobBusy || hasRunning}
              title="一次从维基百科新增几篇文章，可输入 1~100 的任意整数"
            />
            <span className="count-unit">篇</span>
            <button
              className="action-btn"
              onClick={() => runJob("collect", collectCount)}
              disabled={jobBusy || hasRunning}
              title="从维基百科采集文章，自动抽取知识并更新图谱"
            >
              增加并更新图谱
            </button>
          </div>
          <button
            className="action-btn primary"
            onClick={() => runJob("extract")}
            disabled={jobBusy || hasRunning}
            title="对 documents.db 现有文档重新做信息抽取并导出图谱"
          >
            提取现有数据
          </button>
          {(hasRunning || latestJob) && <JobPanel job={latestJob} running={hasRunning} />}
        </div>
        <span className="local-badge">
          <i /> 本地数据 · 可更新
        </span>
      </header>
      <main>
        <div className="intro">
          <div>
            <p className="eyebrow">从实体出发，让知识相连</p>
            <h1>探索医学知识的关联</h1>
            <p>浏览自动抽取的实体与关系，回到原文查看每一条证据。</p>
          </div>
          <span className="dataset-label">
            数据源 <code>triples.json</code>
          </span>
        </div>
        {jobError && (
          <div className="job-error" role="alert">
            {jobError}
            {!jobError.includes("已有任务正在运行") &&
              !jobError.includes("HTTP 409") &&
              !jobError.includes("python server.py") && (
                <>
                  {" "}
                  请确认已运行 <code>python server.py</code>（默认端口 8756）。
                </>
              )}
          </div>
        )}
        {!graph ? (
          <section className="load-panel" role={error ? "alert" : "status"}>
            <h2>{error ? "暂时无法加载图谱" : "正在整理实体与关系…"}</h2>
            <p>{error || "正在读取本地三元组文件"}</p>
            {error && (
              <button
                className="primary"
                onClick={() => setAttempt((a) => a + 1)}
              >
                重新加载
              </button>
            )}
          </section>
          ) : (
          <>
            <section className="stats" aria-label="数据概览">
              {[
                [graph.nodes.length, "去重实体", "ENTITIES"],
                [graph.edges.length, "去重关系", "RELATIONS"],
                [graph.recordCount, "三元组记录", "TRIPLES"],
                [graph.documentCount, "涉及来源文档", "SOURCES"],
              ].map(([count, label, en], i) => (
                <div className="stat" key={label}>
                  <span className="stat-index">0{i + 1}</span>
                  <div>
                    <span className="stat-label">{label}</span>
                    <strong>{Number(count).toLocaleString()}</strong>
                  </div>
                  <small>{en}</small>
                </div>
              ))}
            </section>
            {graph.skipped > 0 && (
              <p className="notice" role="status">
                已跳过 {graph.skipped}{" "}
                条缺少有效主语、关系或宾语的记录；统计中的三元组记录数包含这些原始记录。
              </p>
            )}
            {!graph.nodes.length && (
              <p className="notice">
                数据中暂无有效三元组。请更新数据文件并重新启动或构建前端。
              </p>
            )}
            <section className="workbench">
              <aside className="sidebar">
                <div className="panel-heading">
                  <h2>查找实体</h2>
                  <span>01</span>
                </div>
                <label className="search-box">
                  <span aria-hidden="true">⌕</span>
                  <input
                    aria-label="搜索实体名称"
                    placeholder="输入疾病、症状、药物…"
                    value={query}
                    onChange={(e) => {
                      setQuery(e.target.value);
                      setResultLimit(40);
                    }}
                  />
                  {query && (
                    <button aria-label="清空搜索" onClick={() => setQuery("")}>
                      ×
                    </button>
                  )}
                </label>
                <div className="results-heading">
                  {query ? "搜索结果" : "实体目录"}
                  <span>{results.length} 个</span>
                </div>
                <div className="results" aria-live="polite">
                  {!results.length && (
                    <p className="muted">没有匹配实体，请调整关键词或筛选。</p>
                  )}
                  {results.slice(0, resultLimit).map((n) => (
                    <button
                      className={`entity-result ${n.id === selected?.id ? "active" : ""}`}
                      key={n.id}
                      onClick={() => pickNode(n.id)}
                      title={n.name}
                    >
                      <span>{n.name}</span>
                      <small>{typeLabel(n.type)}</small>
                    </button>
                  ))}
                  {results.length > resultLimit && (
                    <button
                      className="more"
                      onClick={() => setResultLimit((n) => n + 40)}
                    >
                      加载更多（剩余 {results.length - resultLimit}）
                    </button>
                  )}
                </div>
                <fieldset>
                  <legend>
                    实体类型{" "}
                    <button
                      onClick={() => {
                        setChosenTypes(types);
                        setEdgeId(null);
                      }}
                    >
                      全选
                    </button>
                  </legend>
                  <div className="type-list">
                    {types.map((type, index) => (
                      <label key={type}>
                        <input
                          type="checkbox"
                          checked={chosenTypes.includes(type)}
                          onChange={() =>
                            toggle(type, chosenTypes, setChosenTypes)
                          }
                        />
                        <i
                          style={{
                            background: colorForType(type, index),
                          }}
                        />
                        {typeLabel(type)}
                        <small>
                          {graph.nodes.filter((n) => n.type === type).length}
                        </small>
                      </label>
                    ))}
                  </div>
                </fieldset>
                <fieldset>
                  <legend>
                    关系类型{" "}
                    <button
                      onClick={() => {
                        setChosenRelations(relations);
                        setEdgeId(null);
                      }}
                    >
                      全选
                    </button>
                  </legend>
                  <div className="relation-filters">
                    {relations.map((r) => (
                      <label
                        key={r}
                        className={chosenRelations.includes(r) ? "checked" : ""}
                      >
                        <input
                          type="checkbox"
                          checked={chosenRelations.includes(r)}
                          onChange={() =>
                            toggle(r, chosenRelations, setChosenRelations)
                          }
                        />
                        {relationLabel(r)}
                      </label>
                    ))}
                  </div>
                </fieldset>
              </aside>
              <section className="graph-panel">
                <div className="graph-heading">
                  <div>
                    <h2>
                      关系图谱 <span className="chip">{MODE_META[graphMode].chip}</span>
                    </h2>
                    <p>
                      {graphMode === "focus" && selected ? (
                        <>
                          以 <strong>{selected.name}</strong> 为中心 · 点击连线查看证据
                        </>
                      ) : (
                        MODE_META[graphMode].description
                      )}
                    </p>
                  </div>
                  <div className="graph-actions">
                    <div className="graph-mode-switch" role="group" aria-label="图谱视图模式">
                      {(Object.entries(MODE_META) as [GraphMode, (typeof MODE_META)[GraphMode]][]).map(([mode, meta]) => (
                        <button
                          key={mode}
                          className={graphMode === mode ? "active" : ""}
                          onClick={() => {
                            setGraphMode(mode);
                            if (mode !== "focus") setFocus(null);
                            setEdgeId(null);
                          }}
                        >
                          {meta.label}
                        </button>
                      ))}
                    </div>
                    <div className="graph-utility-switch">
                      <button
                        className={legendOpen ? "active" : ""}
                        aria-expanded={legendOpen}
                        onClick={() => setLegendOpen((open) => !open)}
                      >
                        ◫ 图例与方向
                      </button>
                      <button className="reset" onClick={reset}>↺ 恢复默认</button>
                    </div>
                  </div>
                </div>
                {graphMode === "confidence" && (
                  <div className="confidence-control">
                    <label htmlFor="confidence-threshold">
                      最低置信度 <strong>{Math.round(confidenceThreshold * 100)}%</strong>
                    </label>
                    <input
                      id="confidence-threshold"
                      type="range"
                      min="0.5"
                      max="0.81"
                      step="0.01"
                      value={confidenceThreshold}
                      onChange={(event) => {
                        setConfidenceThreshold(Number(event.target.value));
                        setEdgeId(null);
                      }}
                    />
                    <span>
                      当前满足条件：{view?.totalEdges?.toLocaleString() ?? 0} 条关系
                    </span>
                  </div>
                )}
                {legendOpen && (
                  <div className="graph-legend-panel" role="region" aria-label="图例与关系方向说明">
                    <div className="legend-section entity-legend">
                      <strong>实体类型</strong>
                      <div>
                        {types.map((type, index) => (
                          <span key={type}>
                            <i
                              className={`entity-swatch ${shapeForType(type)}`}
                              style={{ background: colorForType(type, index) }}
                            />
                            {typeLabel(type)}
                          </span>
                        ))}
                      </div>
                    </div>
                    <div className="legend-section">
                      <strong>关系置信度</strong>
                      <span><i className="legend-line high" />高可信 ≥ 80%</span>
                      <span><i className="legend-line medium" />中可信 60%～79%</span>
                      <span><i className="legend-line review" />待核验 &lt; 60%</span>
                    </div>
                    <div className="legend-section direction-explainer">
                      <strong>为什么有箭头？</strong>
                      <span><b>疾病</b> —常见症状→ <b>症状</b></span>
                      <small>箭头从三元组主语指向宾语；“相关关系”和派生的“共享症状”是对称关系，因此使用无箭头直线。</small>
                    </div>
                  </div>
                )}
                {view && (
                  <GraphCanvas
                    key={resetKey}
                    view={view}
                    types={types}
                    selectedEdge={selectedEdge?.id ?? null}
                    onNode={pickNode}
                    onEdge={setEdgeId}
                    mode={graphMode}
                  />
                )}
                {view?.note && <div className="mode-note">{view.note}</div>}
                {view && view.totalNodes > view.nodes.length && (
                  <div className="limit-notice" role="status">
                    此模式涉及 {view.totalNodes.toLocaleString()} 个实体
                    {view.totalEdges !== undefined && <>、{view.totalEdges.toLocaleString()} 条关系</>}；
                    为保持交互流畅，当前展示 {view.nodes.length.toLocaleString()} 个实体、
                    {view.edges.length.toLocaleString()} 条关系。可通过搜索、筛选或聚焦继续查看。
                  </div>
                )}
                <div className="graph-footer">
                  <span>
                    当前视图 <b>{view?.nodes.length ?? 0}</b> 个实体 ·{" "}
                    <b>{view?.edges.length ?? 0}</b> 条关系
                  </span>
                  <span>
                    {graphMode === "similarity"
                      ? "无箭头：连线表示疾病共享症状"
                      : "箭头：主语 → 关系 → 宾语；对称关系不加箭头"}
                  </span>
                </div>
              </section>
              <aside className="details">
                <div className="panel-heading">
                  <h2>
                    {selectedEdge
                      ? selectedEdge.derived
                        ? "相似关系说明"
                        : "关系证据"
                      : "实体详情"}
                  </h2>
                  <span>02</span>
                </div>
                {selectedEdge ? (
                  <>
                    <button className="back" onClick={() => setEdgeId(null)}>
                      ← 返回实体详情
                    </button>
                    <div className="evidence-title">
                      <strong>{names.get(selectedEdge.source)}</strong>
                      <span>
                        {selectedEdge.derived ? "—" : "↓"} {relationLabel(selectedEdge.relation)}
                      </span>
                      <strong>{names.get(selectedEdge.target)}</strong>
                    </div>
                    {selectedEdge.derived ? (
                      <div className="derived-edge">
                        <span>前端派生分析</span>
                        <strong>
                          共享 {selectedEdge.weight ?? selectedEdge.sharedNames?.length ?? 0} 个症状
                        </strong>
                        <div>
                          {selectedEdge.sharedNames?.map((name) => (
                            <i key={name}>{name}</i>
                          ))}
                        </div>
                        <p>
                          这条线由已有“疾病—症状”关系计算得到，不是抽取出的新事实，也不代表医学诊断结论。
                        </p>
                      </div>
                    ) : (
                      <>
                        <div className={`confidence-badge ${selectedEdge.confidence < 0.6 ? "review" : selectedEdge.confidence < 0.8 ? "medium" : "high"}`}>
                          置信度 {Math.round(selectedEdge.confidence * 100)}% · {confidenceLabel(selectedEdge.confidence)}
                        </div>
                        <div className="section-label">
                          原文证据 <span>{selectedEdge.evidence.length} 条</span>
                        </div>
                        {selectedEdge.evidence.length ? (
                          selectedEdge.evidence.map((e, index) => (
                            <EvidenceBlock
                              key={index}
                              index={index}
                              documentId={e.documentId}
                              text={e.text}
                            />
                          ))
                        ) : (
                          <p className="muted">暂无来源信息</p>
                        )}
                        <p className="evidence-note">
                          以上为抽取时保留的原句，不代表人工核验结论。
                        </p>
                      </>
                    )}
                  </>
                ) : selected ? (
                  <>
                    <span
                      className="type-badge"
                      style={{
                        color: colorForType(
                          selected.type,
                          types.indexOf(selected.type),
                        ),
                      }}
                    >
                      {typeLabel(selected.type)}
                    </span>
                    <h3 className="entity-name">{selected.name}</h3>
                    <div className="entity-stats">
                      <div><strong>{selectedAllEdges.length}</strong><span>关联三元组</span></div>
                      <div><strong>{new Set(selectedAllEdges.flatMap((e) => e.evidence.map((x) => x.documentId).filter(Boolean))).size}</strong><span>来源文档</span></div>
                      <div><strong>{selectedAllEdges.reduce((n, e) => n + e.evidence.length, 0)}</strong><span>证据片段</span></div>
                    </div>
                    <div className="section-label">
                      关联知识 <span>{selectedAllEdges.length} 条 · 点击查看原句</span>
                    </div>
                    {selectedAllEdges.some((e) => e.confidence < 0.6) && (
                      <p className="quality-warning">部分关联知识缺少来源或存在类型问题，请优先核验橙色虚线关系。</p>
                    )}
                    <div className="related-list">
                      {relationGroups.map(([relation, edges]) => (
                        <div className="relation-group" key={relation}>
                          <h4>{relationLabel(relation)} <span>{edges.length}</span></h4>
                          {edges.map((e) => {
                            const other = e.source === selected.id ? e.target : e.source;
                            return (
                              <article className="related" key={e.id}>
                            <button
                              className="relation-link"
                              onClick={() => setEdgeId(e.id)}
                            >
                              <span className="direction">
                                {e.relation === "RELATED_TO"
                                  ? "—"
                                  : e.source === selected.id
                                    ? "↗"
                                    : "↙"}
                              </span>
                              <span>查看证据 ›</span>
                            </button>
                            <button
                              className="neighbor"
                              onClick={() => pickNode(other)}
                            >
                              {names.get(other)} <small>{typeLabel(graph?.nodes.find((n) => n.id === other)?.type ?? "")}</small> <span>›</span>
                            </button>
                              </article>
                            );
                          })}
                        </div>
                      ))}
                    </div>
                  </>
                ) : (
                  <p className="muted">
                    从左侧搜索或选择图谱中的实体，查看关联关系。
                  </p>
                )}
              </aside>
            </section>
            <Dashboard graph={graph} />
          </>
        )}
        <footer className="page-footer">
          <span>MedGraph / 知识图谱自动构建系统</span>
          <span>自动抽取结果，仅用于课程展示，不作为医疗建议</span>
        </footer>
      </main>
    </div>
  );
}

function Dashboard({ graph }: { graph: Graph }) {
  const { typeCounts, relationCounts, core, isolated, lowQuality } = useMemo(() => {
    const typeMap = new Map<string, number>();
    const relationMap = new Map<string, number>();
    const degree = new Map(graph.nodes.map((node) => [node.id, 0]));
    for (const node of graph.nodes)
      typeMap.set(node.type, (typeMap.get(node.type) ?? 0) + 1);
    let low = 0;
    for (const edge of graph.edges) {
      relationMap.set(edge.relation, (relationMap.get(edge.relation) ?? 0) + 1);
      degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
      if (edge.source !== edge.target)
        degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
      if (edge.confidence < 0.6) low++;
    }
    return {
      typeCounts: [...typeMap.entries()].sort((a, b) => b[1] - a[1]),
      relationCounts: [...relationMap.entries()].sort((a, b) => b[1] - a[1]),
      core: graph.nodes
        .map((node) => ({ ...node, degree: degree.get(node.id) ?? 0 }))
        .sort(
          (a, b) =>
            b.degree - a.degree || a.name.localeCompare(b.name, "zh-CN"),
        )
        .slice(0, 5),
      isolated: graph.nodes.filter((node) => (degree.get(node.id) ?? 0) === 0).length,
      lowQuality: low,
    };
  }, [graph]);
  const typeTotal = typeCounts.reduce((sum, [, count]) => sum + count, 0) || 1;
  const relationTotal = relationCounts.reduce((sum, [, count]) => sum + count, 0) || 1;
  const makeGradient = (items: [string, number][], colors: string[]) => {
    let cursor = 0;
    return `conic-gradient(${items.slice(0, 6).map(([_, count], index) => {
      const start = cursor;
      cursor += (count / (items.reduce((s, [, c]) => s + c, 0) || 1)) * 360;
      return `${colors[index % colors.length]} ${start}deg ${cursor}deg`;
    }).join(",")})`;
  };
  return (
    <section className="dashboard" aria-label="图谱统计分析">
      <div className="dashboard-heading">
        <div><p className="eyebrow">DATA INSIGHTS</p><h2>图谱统计分析</h2></div>
        <span>基于当前已导入的三元组实时计算</span>
      </div>
      <div className="donut-grid">
        <div className="donut-card"><div className="donut" style={{ background: makeGradient(typeCounts, ["#1b806f", "#d69134", "#6382c1", "#ad73a4", "#7c9460", "#8896a0"]) }}><span>{graph.nodes.length.toLocaleString()}<small>实体</small></span></div><div className="donut-caption"><strong>实体构成</strong>{typeCounts.slice(0, 4).map(([type, count]) => <span key={type}><i style={{ background: ["#1b806f", "#d69134", "#6382c1", "#ad73a4"][typeCounts.findIndex((x) => x[0] === type) % 4] }} />{typeLabel(type)} {Math.round((count / typeTotal) * 100)}%</span>)}</div></div>
        <div className="donut-card"><div className="donut" style={{ background: makeGradient(relationCounts, ["#7653b6", "#4d79bd", "#d69134", "#4a9a91", "#ad73a4", "#8896a0"]) }}><span>{graph.edges.length.toLocaleString()}<small>关系</small></span></div><div className="donut-caption"><strong>关系构成</strong>{relationCounts.slice(0, 4).map(([relation, count], index) => <span key={relation}><i style={{ background: ["#7653b6", "#4d79bd", "#d69134", "#4a9a91"][index] }} />{relationLabel(relation)} {Math.round((count / relationTotal) * 100)}%</span>)}</div></div>
      </div>
      <div className="dashboard-grid">
        <div className="dash-card dash-wide">
          <h3>实体类型分布</h3>
          <div className="bar-list">{typeCounts.slice(0, 8).map(([type, count]) => (
            <div className="bar-row" key={type}><span>{typeLabel(type)}</span><div className="bar-track"><i style={{ width: `${(count / typeTotal) * 100}%` }} /></div><b>{count.toLocaleString()}</b></div>
          ))}</div>
        </div>
        <div className="dash-card dash-wide">
          <h3>关系类型分布</h3>
          <div className="bar-list">{relationCounts.slice(0, 8).map(([relation, count]) => (
            <div className="bar-row" key={relation}><span>{relationLabel(relation)}</span><div className="bar-track purple"><i style={{ width: `${(count / relationTotal) * 100}%` }} /></div><b>{count.toLocaleString()}</b></div>
          ))}</div>
        </div>
        <div className="dash-card">
          <h3>质量与覆盖</h3>
          <div className="quality-grid">
            <div><strong>{graph.documentCount.toLocaleString()}</strong><span>来源文档</span></div>
            <div><strong>{graph.edges.length ? Math.round((graph.edges.filter((e) => e.evidence.length > 0).length / graph.edges.length) * 100) : 0}%</strong><span>证据覆盖率</span></div>
            <div><strong>{isolated}</strong><span>孤立节点</span></div>
            <div className="warning"><strong>{lowQuality.toLocaleString()}</strong><span>待核验知识</span></div>
          </div>
          <small className="dash-note">证据覆盖率按带有原文片段的关联三元组计算</small>
        </div>
        <div className="dash-card">
          <h3>核心实体 TOP 5</h3>
          <div className="core-list">{core.map((n, index) => (
            <div key={n.id}><em>{String(index + 1).padStart(2, "0")}</em><span>{n.name}<small>{typeLabel(n.type)}</small></span><b>{n.degree}</b></div>
          ))}</div>
        </div>
      </div>
      <p className="dashboard-footnote">类别覆盖：当前图谱文件未携带文档 category_ids，类别统计需从采集文档索引单独读取。</p>
    </section>
  );
}

function EvidenceBlock({
  index,
  documentId,
  text,
}: {
  index: number;
  documentId: string;
  text: string;
}) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<
    "idle" | "loading" | "ready" | "missing" | "error"
  >("idle");
  const [record, setRecord] = useState<DocumentRecord | null>(null);

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (!next || status !== "idle" || !documentId) return;
    setStatus("loading");
    try {
      const doc = await fetchDocument(documentId);
      if (doc) {
        setRecord(doc);
        setStatus("ready");
      } else {
        setStatus("missing");
      }
    } catch {
      setStatus("error");
    }
  };

  return (
    <article className="evidence">
      <div>
        <span>来源 {String(index + 1).padStart(2, "0")}</span>
        <span className="evidence-meta">
          <code>{documentId || "暂无文档编号"}</code>
          {documentId && (
            <button
              type="button"
              className="doc-expand"
              onClick={toggle}
              disabled={status === "loading"}
            >
              {status === "loading" ? "加载中…" : open ? "收起" : "展开"}
            </button>
          )}
        </span>
      </div>
      <blockquote>{text || "暂无来源原句"}</blockquote>
      {open && documentId && (
        <div className="doc-original">
          {status === "loading" && <p className="muted">正在加载文档原文…</p>}
          {status === "missing" && (
            <p className="muted">
              当前数据包未包含 {documentId} 的完整文档；上方抽取原句仍可用于核对。
            </p>
          )}
          {status === "error" && (
            <p className="muted">
              当前数据包未同步完整文档；上方抽取原句仍可用于核对。
            </p>
          )}
          {status === "ready" && record && (
            <>
              <h5>{record.title || documentId}</h5>
              <p>{record.content || "（该文档暂无正文）"}</p>
            </>
          )}
        </div>
      )}
    </article>
  );
}

function JobPanel({ job, running }: { job: Job; running: boolean }) {
  const label = job.kind === "extract" ? "提取数据" : "增加数据";
  return (
    <div className={`job-panel ${job.status}`} role="status">
      <span className="job-dot" />
      <div className="job-info">
        <strong>
          {label}
          {job.status === "running" && " · 进行中"}
          {job.status === "succeeded" && " · 完成"}
          {job.status === "failed" && " · 失败"}
        </strong>
        <span>
          {running ? job.message : job.message || job.finished_at || "任务已结束"}
        </span>
        {job.status === "failed" && job.detail && (
          <span className="job-detail" title={job.detail}>
            {job.detail.length > 200
              ? `${job.detail.slice(0, 200)}…`
              : job.detail}
          </span>
        )}
      </div>
    </div>
  );
}
