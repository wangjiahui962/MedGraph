import { describe, it, expect } from "vitest";
import { parseGraph, filterGraph, neighborhood, searchNodes } from "./graph";

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
});
