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
}
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
    };
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
    nodes: graph.nodes.filter((n) => connected.has(n.id)),
    edges,
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
