export interface Entity {
  id: string;
  name: string;
  type: string;
}
export interface Evidence {
  documentId: string;
  text: string;
}
export interface Relation {
  id: string;
  source: string;
  target: string;
  relation: string;
  evidence: Evidence[];
  confidence: number;
  qualityFlags: string[];
  derived?: boolean;
  weight?: number;
  sharedNames?: string[];
}
export interface Graph {
  nodes: Entity[];
  edges: Relation[];
  recordCount: number;
  documentCount: number;
  skipped: number;
}
export interface GraphView {
  nodes: Entity[];
  edges: Relation[];
  focusId: string | null;
  totalNodes: number;
  totalEdges?: number;
  note?: string;
}
export type GraphMode =
  | "overview"
  | "focus"
  | "confidence"
  | "bipartite"
  | "similarity"
  | "full";
const clean = (v: unknown) => (typeof v === "string" ? v.trim() : "");
const compare = (a: Entity, b: Entity) =>
  a.name.localeCompare(b.name, "zh-CN") ||
  a.type.localeCompare(b.type, "zh-CN") ||
  a.id.localeCompare(b.id);

export function parseGraph(text: string): Graph {
  let records: unknown;
  try {
    records = JSON.parse(text);
  } catch {
    throw new Error("数据不是有效的 JSON，请检查 triples.json。");
  }
  if (!Array.isArray(records)) throw new Error("三元组数据必须是 JSON 数组。");
  const nodes = new Map<string, Entity>();
  const edges = new Map<string, Relation>();
  const documents = new Set<string>();
  let skipped = 0;
  for (const value of records) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      skipped++;
      continue;
    }
    const row = value as Record<string, unknown>;
    const subject = clean(row.subject),
      object = clean(row.object),
      relation = clean(row.relation);
    if (!subject || !object || !relation) {
      skipped++;
      continue;
    }
    const subjectType = clean(row.subject_type) || "未分类",
      objectType = clean(row.object_type) || "未分类";
    const source = JSON.stringify([subject, subjectType]),
      target = JSON.stringify([object, objectType]);
    nodes.set(source, { id: source, name: subject, type: subjectType });
    nodes.set(target, { id: target, name: object, type: objectType });
    const id = JSON.stringify([source, relation, target]);
    const edge = edges.get(id) ?? {
      id,
      source,
      target,
      relation,
      evidence: [],
      confidence: 1,
      qualityFlags: [],
    };
    const confidence = typeof row.confidence === "number" ? row.confidence : 0.72;
    edge.confidence = Math.min(edge.confidence, confidence);
    const flags = Array.isArray(row.quality_flags) ? row.quality_flags.filter((v): v is string => typeof v === "string") : [];
    edge.qualityFlags = [...new Set([...edge.qualityFlags, ...flags])];
    const evidence = {
      documentId: clean(row.source_document_id),
      text: clean(row.source_text),
    };
    if (evidence.documentId) documents.add(evidence.documentId);
    if (
      (evidence.documentId || evidence.text) &&
      !edge.evidence.some(
        (e) => e.documentId === evidence.documentId && e.text === evidence.text,
      )
    )
      edge.evidence.push(evidence);
    edges.set(id, edge);
  }
  return {
    nodes: [...nodes.values()].sort(compare),
    edges: [...edges.values()],
    recordCount: records.length,
    documentCount: documents.size,
    skipped,
  };
}

export async function loadGraph(signal?: AbortSignal): Promise<Graph> {
  const response = await fetch(`${import.meta.env.BASE_URL}data/triples.json`, {
    signal,
    cache: "no-store",
  });
  if (!response.ok)
    throw new Error(
      `数据加载失败（HTTP ${response.status}），请确认已同步 triples.json。`,
    );
  return parseGraph(await response.text());
}

export function filterGraph(
  graph: Graph,
  types: string[],
  relations: string[],
  preserveIsolated = false,
): Graph {
  const ids = new Set(
    graph.nodes.filter((n) => types.includes(n.type)).map((n) => n.id),
  );
  const edges = graph.edges.filter(
    (e) =>
      ids.has(e.source) && ids.has(e.target) && relations.includes(e.relation),
  );
  const connected = new Set(edges.flatMap((e) => [e.source, e.target]));
  return {
    ...graph,
    nodes: preserveIsolated
      ? graph.nodes.filter((n) => ids.has(n.id))
      : graph.nodes.filter((n) => connected.has(n.id)),
    edges,
  };
}

function degreeMap(nodes: Entity[], edges: Relation[]) {
  const degree = new Map(nodes.map((node) => [node.id, 0]));
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    if (edge.source !== edge.target)
      degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }
  return degree;
}

function representativeView(
  graph: Graph,
  maxNodes: number,
  maxEdges: number,
  note?: string,
): GraphView {
  const degree = degreeMap(graph.nodes, graph.edges);
  const ranked = [...graph.nodes].sort(
    (a, b) =>
      (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0) || compare(a, b),
  );
  const nodes = ranked.slice(0, maxNodes);
  const visible = new Set(nodes.map((node) => node.id));
  const edges = graph.edges
    .filter((edge) => visible.has(edge.source) && visible.has(edge.target))
    .sort(
      (a, b) =>
        (degree.get(b.source) ?? 0) + (degree.get(b.target) ?? 0) -
          (degree.get(a.source) ?? 0) -
          (degree.get(a.target) ?? 0) ||
        b.confidence - a.confidence ||
        a.id.localeCompare(b.id),
    )
    .slice(0, maxEdges);
  return {
    nodes,
    edges,
    focusId: null,
    totalNodes: graph.nodes.length,
    totalEdges: graph.edges.length,
    note,
  };
}

/** 快速首屏：保留高连接度骨架，完整数据仍可通过 fullGraph 查看。 */
export function overview(
  graph: Graph,
  maxNodes = 420,
  maxEdges = 1200,
): GraphView {
  return representativeView(
    graph,
    maxNodes,
    maxEdges,
    "按连接度展示代表性结构；搜索、筛选和详情仍使用完整数据。",
  );
}

export function fullGraph(graph: Graph): GraphView {
  return {
    nodes: graph.nodes,
    edges: graph.edges,
    focusId: null,
    totalNodes: graph.nodes.length,
    totalEdges: graph.edges.length,
    note: "全量模式使用快速同心布局，适合观察总体规模，不适合逐条阅读。",
  };
}

export function confidenceView(
  graph: Graph,
  threshold: number,
  maxNodes = 520,
  maxEdges = 1500,
): GraphView {
  const edges = graph.edges.filter((edge) => edge.confidence >= threshold);
  const connected = new Set(edges.flatMap((edge) => [edge.source, edge.target]));
  const confidenceGraph: Graph = {
    ...graph,
    nodes: graph.nodes.filter((node) => connected.has(node.id)),
    edges,
  };
  return representativeView(
    confidenceGraph,
    maxNodes,
    maxEdges,
    `当前仅显示置信度不低于 ${Math.round(threshold * 100)}% 的关系。`,
  );
}

export function bipartiteView(
  graph: Graph,
  maxDiseases = 110,
  maxSymptoms = 70,
  maxEdges = 800,
): GraphView {
  const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
  const symptomEdges = graph.edges.filter((edge) => {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    return (
      edge.relation === "HAS_SYMPTOM" &&
      source?.type === "Disease" &&
      target?.type === "Symptom"
    );
  });
  const degree = degreeMap(graph.nodes, symptomEdges);
  const diseases = graph.nodes
    .filter((node) => node.type === "Disease" && (degree.get(node.id) ?? 0) > 0)
    .sort(
      (a, b) =>
        (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0) || compare(a, b),
    );
  const symptoms = graph.nodes
    .filter((node) => node.type === "Symptom" && (degree.get(node.id) ?? 0) > 0)
    .sort(
      (a, b) =>
        (degree.get(b.id) ?? 0) - (degree.get(a.id) ?? 0) || compare(a, b),
    );
  const chosenDiseases = diseases.slice(0, maxDiseases);
  const chosenSymptoms = symptoms.slice(0, maxSymptoms);
  const visible = new Set(
    [...chosenDiseases, ...chosenSymptoms].map((node) => node.id),
  );
  return {
    nodes: [...chosenDiseases, ...chosenSymptoms],
    edges: symptomEdges
      .filter((edge) => visible.has(edge.source) && visible.has(edge.target))
      .slice(0, maxEdges),
    focusId: null,
    totalNodes: diseases.length + symptoms.length,
    totalEdges: symptomEdges.length,
    note: "左侧为疾病、右侧为症状；共享同一症状的疾病会汇聚到同一节点。",
  };
}

export function similarityView(
  graph: Graph,
  maxDiseases = 120,
  maxEdges = 220,
): GraphView {
  const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
  const diseasesBySymptom = new Map<string, Set<string>>();
  for (const edge of graph.edges) {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (
      edge.relation !== "HAS_SYMPTOM" ||
      source?.type !== "Disease" ||
      target?.type !== "Symptom"
    )
      continue;
    const bucket = diseasesBySymptom.get(target.id) ?? new Set<string>();
    bucket.add(source.id);
    diseasesBySymptom.set(target.id, bucket);
  }
  const pairs = new Map<
    string,
    { source: string; target: string; shared: string[] }
  >();
  for (const [symptomId, diseaseSet] of diseasesBySymptom) {
    const diseaseIds = [...diseaseSet].sort();
    const symptomName = nodeById.get(symptomId)?.name ?? symptomId;
    for (let i = 0; i < diseaseIds.length; i++) {
      for (let j = i + 1; j < diseaseIds.length; j++) {
        const source = diseaseIds[i];
        const target = diseaseIds[j];
        const key = JSON.stringify([source, target]);
        const pair = pairs.get(key) ?? { source, target, shared: [] };
        pair.shared.push(symptomName);
        pairs.set(key, pair);
      }
    }
  }
  const rankedPairs = [...pairs.values()]
    .filter((pair) => pair.shared.length >= 2)
    .sort(
      (a, b) =>
        b.shared.length - a.shared.length ||
        a.source.localeCompare(b.source) ||
        a.target.localeCompare(b.target),
    );
  const diseaseDegree = new Map<string, number>();
  for (const pair of rankedPairs) {
    diseaseDegree.set(pair.source, (diseaseDegree.get(pair.source) ?? 0) + pair.shared.length);
    diseaseDegree.set(pair.target, (diseaseDegree.get(pair.target) ?? 0) + pair.shared.length);
  }
  const chosenIds = new Set(
    [...diseaseDegree.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, maxDiseases)
      .map(([id]) => id),
  );
  const edges: Relation[] = rankedPairs
    .filter((pair) => chosenIds.has(pair.source) && chosenIds.has(pair.target))
    .slice(0, maxEdges)
    .map((pair) => ({
      id: JSON.stringify(["derived", pair.source, pair.target]),
      source: pair.source,
      target: pair.target,
      relation: "SHARES_SYMPTOM",
      evidence: [],
      confidence: 1,
      qualityFlags: [],
      derived: true,
      weight: pair.shared.length,
      sharedNames: pair.shared.sort((a, b) => a.localeCompare(b, "zh-CN")),
    }));
  const used = new Set(edges.flatMap((edge) => [edge.source, edge.target]));
  return {
    nodes: graph.nodes.filter((node) => used.has(node.id)),
    edges,
    focusId: null,
    totalNodes: diseaseDegree.size,
    totalEdges: rankedPairs.length,
    note: "无箭头；边表示两种疾病至少共享 2 个症状，线越粗表示共享越多。此视图为前端派生分析。",
  };
}
export function searchNodes(graph: Pick<Graph, "nodes">, query: string) {
  const keyword = query.trim().toLocaleLowerCase();
  return graph.nodes
    .filter((n) => n.name.toLocaleLowerCase().includes(keyword))
    .sort(compare);
}
export function neighborhood(
  graph: Graph,
  requestedId?: string | null,
  limit = 80,
): GraphView {
  const degree = new Map(graph.nodes.map((n) => [n.id, 0]));
  graph.edges.forEach((e) => {
    degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
    if (e.source !== e.target)
      degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
  });
  const rank = (a: Entity, b: Entity) =>
    degree.get(b.id)! - degree.get(a.id)! || compare(a, b);
  const focus =
    graph.nodes.find((n) => n.id === requestedId) ??
    [...graph.nodes].sort(rank)[0];
  if (!focus) return { nodes: [], edges: [], focusId: null, totalNodes: 0 };
  const adjacent = graph.edges.filter(
    (e) => e.source === focus.id || e.target === focus.id,
  );
  const ids = new Set(adjacent.flatMap((e) => [e.source, e.target]));
  ids.add(focus.id);
  const candidates = graph.nodes
    .filter((n) => ids.has(n.id) && n.id !== focus.id)
    .sort(rank);
  const nodes = [focus, ...candidates].slice(0, Math.max(1, limit));
  const visible = new Set(nodes.map((n) => n.id));
  return {
    nodes,
    edges: graph.edges.filter(
      (e) => visible.has(e.source) && visible.has(e.target),
    ),
    focusId: focus.id,
    totalNodes: ids.size,
  };
}
