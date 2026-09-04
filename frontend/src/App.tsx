import { useEffect, useMemo, useRef, useState } from "react";
import {
  filterGraph,
  loadGraph,
  neighborhood,
  searchNodes,
  type Graph,
} from "./graph";
import { GraphCanvas, palette } from "./GraphCanvas";
import { fetchDocument, type DocumentRecord } from "./documents";
import {
  fetchJobs,
  startCollect,
  startExtract,
  type Job,
} from "./api";

export default function App() {
  const [graph, setGraph] = useState<Graph | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [chosenTypes, setChosenTypes] = useState<string[]>([]);
  const [chosenRelations, setChosenRelations] = useState<string[]>([]);
  const [focus, setFocus] = useState<string | null>(null);
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
  // 记录“曾处于 running”的任务 id：extract 成功结束时据此触发图谱自动刷新
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
        kind === "extract" ? await startExtract() : await startCollect(count);
      if (kind === "extract") runningSeenRef.current.add(started.job_id);
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

  // “提取现有数据”成功结束 → 重新读取 triples.json，避免手动刷新整页
  useEffect(() => {
    const runningNow = new Set(
      jobs.filter((j) => j.status === "running").map((j) => j.id),
    );
    for (const job of jobs) {
      if (
        job.kind === "extract" &&
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
    () => (graph ? filterGraph(graph, chosenTypes, chosenRelations) : null),
    [graph, chosenTypes, chosenRelations],
  );
  const view = useMemo(
    () => (filtered ? neighborhood(filtered, focus) : null),
    [filtered, focus],
  );
  const results = useMemo(
    () => (filtered ? searchNodes(filtered, query) : []),
    [filtered, query],
  );
  const selected = graph?.nodes.find((n) => n.id === view?.focusId);
  const selectedEdge = filtered?.edges.find((e) => e.id === edgeId);
  const related =
    filtered?.edges.filter(
      (e) => e.source === selected?.id || e.target === selected?.id,
    ) ?? [];
  const names = useMemo(
    () => new Map(graph?.nodes.map((n) => [n.id, n.name])),
    [graph],
  );
  function pickNode(id: string) {
    setFocus(id);
    setEdgeId(null);
  }
  function reset() {
    setQuery("");
    setChosenTypes(types);
    setChosenRelations(relations);
    setFocus(null);
    setEdgeId(null);
    setResultLimit(40);
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
              title="从维基百科搜索新增文章并存入 documents.db（繁体自动转简体）"
            >
              增加新数据
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
                      <small>{n.type}</small>
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
                            background: palette[index % palette.length],
                          }}
                        />
                        {type}
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
                        {r}
                      </label>
                    ))}
                  </div>
                </fieldset>
              </aside>
              <section className="graph-panel">
                <div className="graph-heading">
                  <div>
                    <h2>
                      关系图谱 <span className="chip">一跳邻域</span>
                    </h2>
                    <p>
                      {selected ? (
                        <>
                          以 <strong>{selected.name}</strong> 为中心
                        </>
                      ) : (
                        "暂无可展示实体"
                      )}
                    </p>
                  </div>
                  <button className="reset" onClick={reset}>
                    ↺ 恢复默认
                  </button>
                </div>
                {view && (
                  <GraphCanvas
                    key={resetKey}
                    view={view}
                    types={types}
                    selectedEdge={selectedEdge?.id ?? null}
                    onNode={pickNode}
                    onEdge={setEdgeId}
                  />
                )}
                {view && view.totalNodes > view.nodes.length && (
                  <div className="limit-notice" role="status">
                    邻域共 {view.totalNodes} 个实体，当前仅展示前{" "}
                    {view.nodes.length}{" "}
                    个。请搜索或筛选缩小范围；右侧列表保留全部关联关系。
                  </div>
                )}
                <div className="graph-footer">
                  <span>
                    当前视图 <b>{view?.nodes.length ?? 0}</b> 个实体 ·{" "}
                    <b>{view?.edges.length ?? 0}</b> 条关系
                  </span>
                  <span>箭头表示关系方向</span>
                </div>
              </section>
              <aside className="details">
                <div className="panel-heading">
                  <h2>{selectedEdge ? "关系证据" : "实体详情"}</h2>
                  <span>02</span>
                </div>
                {selectedEdge ? (
                  <>
                    <button className="back" onClick={() => setEdgeId(null)}>
                      ← 返回实体详情
                    </button>
                    <div className="evidence-title">
                      <strong>{names.get(selectedEdge.source)}</strong>
                      <span>↓ {selectedEdge.relation}</span>
                      <strong>{names.get(selectedEdge.target)}</strong>
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
                ) : selected ? (
                  <>
                    <span
                      className="type-badge"
                      style={{
                        color:
                          palette[
                          types.indexOf(selected.type) % palette.length
                          ],
                      }}
                    >
                      {selected.type}
                    </span>
                    <h3 className="entity-name">{selected.name}</h3>
                    <p className="muted">
                      当前筛选下关联 {related.length} 条关系
                    </p>
                    <div className="section-label">
                      关联关系 <span>点击查看原句</span>
                    </div>
                    <div className="related-list">
                      {related.map((e) => {
                        const other =
                          e.source === selected.id ? e.target : e.source;
                        return (
                          <article className="related" key={e.id}>
                            <button
                              className="relation-link"
                              onClick={() => setEdgeId(e.id)}
                            >
                              {e.source === selected.id ? "↗" : "↙"}{" "}
                              {e.relation}
                              <span>查看证据 ›</span>
                            </button>
                            <button
                              className="neighbor"
                              onClick={() => pickNode(other)}
                            >
                              {names.get(other)} <span>→</span>
                            </button>
                          </article>
                        );
                      })}
                    </div>
                  </>
                ) : (
                  <p className="muted">
                    从左侧搜索或选择图谱中的实体，查看关联关系。
                  </p>
                )}
              </aside>
            </section>
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
            <p className="muted">未找到 {documentId} 的原文数据。</p>
          )}
          {status === "error" && (
            <p className="muted">
              原文加载失败，请确认已生成并同步 documents.json。
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
