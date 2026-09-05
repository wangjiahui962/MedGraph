import { describe, it, expect } from "vitest";
import {
  bipartiteView,
  confidenceView,
  filterGraph,
  neighborhood,
  overview,
  parseGraph,
  searchNodes,
  similarityView,
} from "./graph";

const triple = {
  subject: "甲",
  subject_type: "疾病",
  relation: "表现",
  object: "乙",
  object_type: "症状",
  source_document_id: "doc1",
  source_text: "原句",
};
describe("数据适配", () => {
  it("合并重复边，保留不同证据并正确统计", () => {
    const g = parseGraph(
      JSON.stringify([
        triple,
        triple,
        { ...triple, source_document_id: "doc2" },
      ]),
    );
    expect(g.nodes).toHaveLength(2);
    expect(g.edges).toHaveLength(1);
    expect(g.edges[0].evidence).toHaveLength(2);
    expect(g.recordCount).toBe(3);
    expect(g.documentCount).toBe(2);
  });
  it("同名异类不能合并，键不受分隔符影响", () => {
    const g = parseGraph(JSON.stringify([triple, { ...triple, object: "甲" }]));
    expect(g.nodes).toHaveLength(3);
  });
  it("跳过非法记录，类型和证据允许缺失", () => {
    const g = parseGraph(
      JSON.stringify([
        null,
        {},
        { subject: "甲", relation: "关联", object: "乙" },
      ]),
    );
    expect(g.skipped).toBe(2);
    expect(g.nodes[0].type).toBe("未分类");
    expect(g.edges[0].evidence).toEqual([]);
  });
  it("对非法 JSON 和非数组报明确错误", () => {
    expect(() => parseGraph("{")).toThrow("JSON");
    expect(() => parseGraph("{}")).toThrow("数组");
    expect(parseGraph("[]").nodes).toEqual([]);
  });
  it("来源文本保持普通字符串", () => {
    const g = parseGraph(
      JSON.stringify([{ ...triple, source_text: "<script>alert(1)</script>" }]),
    );
    expect(g.edges[0].evidence[0].text).toBe("<script>alert(1)</script>");
  });
});
describe("查询与图谱范围", () => {
  const g = parseGraph(
    JSON.stringify([
      triple,
      { ...triple, object: "丙", object_type: "药物", relation: "治疗" },
    ]),
  );
  it("筛选同时约束关系端点与搜索结果", () => {
    const f = filterGraph(g, ["疾病", "症状"], ["表现", "治疗"]);
    expect(f.edges).toHaveLength(1);
    expect(searchNodes(f, "丙")).toHaveLength(0);
    expect(filterGraph(g, [], []).nodes).toHaveLength(0);
  });
  it("名称搜索去除空白并忽略大小写", () => {
    expect(searchNodes(g, "  甲  ")).toHaveLength(1);
  });
  it("默认选择最高关联度实体，选择后保留一跳邻居", () => {
    const view = neighborhood(g);
    expect(view.nodes.find((n) => n.id === view.focusId)?.name).toBe("甲");
    expect(view.nodes).toHaveLength(3);
  });
  it("大图最多 80 节点并报告被截断的范围，顺序稳定", () => {
    const large = parseGraph(
      JSON.stringify(
        Array.from({ length: 100 }, (_, i) => ({
          ...triple,
          object: `节点${i}`,
        })),
      ),
    );
    const view = neighborhood(large);
    expect(view.nodes).toHaveLength(80);
    expect(view.totalNodes).toBe(101);
    expect(view).toEqual(neighborhood(large));
    expect(view.edges).toHaveLength(79);
  });
  it("空图安全返回", () => {
    expect(neighborhood(parseGraph("[]")).nodes).toEqual([]);
  });
  it("结构概览限制首屏规模并保留完整规模统计", () => {
    const large = parseGraph(
      JSON.stringify(
        Array.from({ length: 160 }, (_, i) => ({ ...triple, object: `概览${i}` })),
      ),
    );
    const view = overview(large, 80, 100);
    expect(view.nodes).toHaveLength(80);
    expect(view.totalNodes).toBe(161);
    expect(view.totalEdges).toBe(160);
    const endpoints = new Set(view.edges.flatMap((edge) => [edge.source, edge.target]));
    expect(view.nodes.every((node) => endpoints.has(node.id))).toBe(true);
  });
  it("可信度阈值可调", () => {
    const quality = parseGraph(
      JSON.stringify([
        { ...triple, confidence: 0.81 },
        { ...triple, object: "丙", confidence: 0.59 },
      ]),
    );
    expect(confidenceView(quality, 0.8).totalEdges).toBe(1);
    expect(confidenceView(quality, 0.55).totalEdges).toBe(2);
  });
  it("疾病—症状二部图只保留对应关系", () => {
    const medical = parseGraph(
      JSON.stringify([
        { ...triple, subject_type: "Disease", object_type: "Symptom", relation: "HAS_SYMPTOM" },
        { ...triple, object: "药物", subject_type: "Disease", object_type: "Drug", relation: "TREATED_BY" },
      ]),
    );
    const view = bipartiteView(medical);
    expect(view.nodes).toHaveLength(2);
    expect(view.edges.map((edge) => edge.relation)).toEqual(["HAS_SYMPTOM"]);
  });
  it("相似疾病视图由至少两个共享症状派生且不伪装成原始证据", () => {
    const rows = ["头痛", "发热"].flatMap((symptom) =>
      ["疾病甲", "疾病乙"].map((disease) => ({
        subject: disease,
        subject_type: "Disease",
        relation: "HAS_SYMPTOM",
        object: symptom,
        object_type: "Symptom",
      })),
    );
    const view = similarityView(parseGraph(JSON.stringify(rows)));
    expect(view.nodes).toHaveLength(2);
    expect(view.edges).toHaveLength(1);
    expect(view.edges[0]).toMatchObject({
      relation: "SHARES_SYMPTOM",
      derived: true,
      weight: 2,
      evidence: [],
    });
  });
});
