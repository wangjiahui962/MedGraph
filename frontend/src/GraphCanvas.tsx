import { useEffect, useRef } from "react";
import cytoscape, { type Core } from "cytoscape";
import fcose from "cytoscape-fcose";
import type { GraphView } from "./graph";

// 注册 fcose 力导向布局插件
cytoscape.use(fcose);

function fitGraph(cy: Core) {
  cy.fit(undefined, 48);
  if (cy.zoom() > 1) {
    cy.zoom(1);
    cy.center();
  }
}

export const palette = [
  "#207c76",
  "#d69134",
  "#6382c1",
  "#ad73a4",
  "#7c9460",
  "#8896a0",
];
export function GraphCanvas({
  view,
  types,
  selectedEdge,
  onNode,
  onEdge,
}: {
  view: GraphView;
  types: string[];
  selectedEdge: string | null;
  onNode: (id: string) => void;
  onEdge: (id: string) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const handlers = useRef({ onNode, onEdge });
  handlers.current = { onNode, onEdge };
  useEffect(() => {
    if (!container.current || !view.nodes.length) return;
    // 计算每个节点的关联度（degree，即连接数），用于节点大小映射
    const degree = new Map(view.nodes.map((n) => [n.id, 0]));
    view.edges.forEach((e) => {
      degree.set(e.source, (degree.get(e.source) ?? 0) + 1);
      if (e.source !== e.target)
        degree.set(e.target, (degree.get(e.target) ?? 0) + 1);
    });
    const cy = cytoscape({
      container: container.current,
      // 高分屏适配：按设备像素比渲染，避免画布/文字在高分屏下发虚
      pixelRatio: window.devicePixelRatio || 1,
      elements: [
        ...view.nodes.map((n, index) => ({
          data: {
            id: n.id,
            label: n.name,
            color: palette[types.indexOf(n.type) % palette.length],
            focus: n.id === view.focusId ? 1 : 0,
            degree: degree.get(n.id) ?? 0,
          },
          position:
            n.id === view.focusId
              ? { x: 0, y: 0 }
              : {
                x:
                  Math.cos(
                    (index * 2 * Math.PI) /
                    Math.max(view.nodes.length - 1, 1),
                  ) * 240,
                y:
                  Math.sin(
                    (index * 2 * Math.PI) /
                    Math.max(view.nodes.length - 1, 1),
                  ) * 240,
              },
        })),
        ...view.edges.map((e) => ({
          data: {
            id: e.id,
            source: e.source,
            target: e.target,
            label: e.relation,
          },
        })),
      ],
      style: [
        {
          selector: "node",
          style: {
            "background-color": "data(color)",
            label: "data(label)",
            color: "#334650",
            // 中文标签优先用黑体类字体（小字号更清晰）；字号 12px
            "font-size": 12,
            "font-family": "Microsoft YaHei, PingFang SC, Noto Sans CJK SC, '楷体', KaiTi, sans-serif",
            "text-valign": "bottom",
            "text-margin-y": 4,
            "text-wrap": "ellipsis",
            "text-max-width": "90px",
            // 节点大小随关联度（degree）缩放：degree 1~15 映射到 7~22px，
            // 核心实体（连接多）更大、边缘实体更小，一眼看出重要节点
            width: "mapData(degree, 1, 15, 7, 22)",
            height: "mapData(degree, 1, 15, 7, 22)",
            "border-width": 0,
            "border-color": "#ffffff",
          },
        },
        {
          selector: "node[focus = 1]",
          style: {
            // 焦点（当前中心）始终最大，便于定位
            width: "mapData(degree, 1, 15, 14, 32)",
            height: "mapData(degree, 1, 15, 14, 32)",
            "border-width": 0,
            "border-color": "#d9ebe8",
            "font-weight": "bold",
            "font-size": 13,
          },
        },
        {
          selector: "edge",
          style: {
            width: 1.3,
            "line-color": "#bed0d1",
            "target-arrow-color": "#9cb6b5",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            // 隐藏连线上的关系文字，避免内圈文字扎堆
            label: "",
          },
        },
        {
          selector: "edge:selected",
          style: {
            width: 3,
            "line-color": "#207c76",
            "target-arrow-color": "#207c76",
            color: "#145c58",
          },
        },
      ],
      layout: {
        // fcose 力导向布局：节点自动避让、分布均衡自然
        // （fcose 专属参数不在 cytoscape 基础布局类型里，此处用断言）
        name: "fcose",
        quality: "default",
        animate: false,
        randomize: false,
        // 布局时把标签尺寸计入节点，避免文字扎堆
        nodeDimensionsIncludeLabels: true,
        // 边弹簧理想长度：适中即可，节点间距离由节点大小 + 斥力共同决定
        idealEdgeLength: 90,
        // 节点斥力：越大分布越松散
        nodeRepulsion: 9000,
        padding: 60,
      } as cytoscape.LayoutOptions,
      minZoom: 0.08,
      maxZoom: 3,
      wheelSensitivity: 0.2,
    });
    cyRef.current = cy;
    fitGraph(cy);
    cy.on("tap", "node", (event) => handlers.current.onNode(event.target.id()));
    cy.on("tap", "edge", (event) => handlers.current.onEdge(event.target.id()));
    const observer = new ResizeObserver(() => cy.resize());
    observer.observe(container.current);
    return () => {
      observer.disconnect();
      cy.destroy();
      cyRef.current = null;
    };
  }, [view, types]);
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.edges().unselect();
    if (selectedEdge) cy.getElementById(selectedEdge).select();
  }, [selectedEdge, view]);
  function zoom(factor: number) {
    const cy = cyRef.current;
    if (cy)
      cy.zoom({
        level: cy.zoom() * factor,
        renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 },
      });
  }
  return (
    <div className="canvas-wrap">
      <div
        ref={container}
        className="graph-canvas"
        role="img"
        aria-label="实体关系图。可使用左侧搜索和右侧关系列表进行键盘操作。"
      />
      {!view.nodes.length && (
        <div className="canvas-empty">
          当前筛选下没有实体
          <br />
          <small>请调整左侧筛选条件</small>
        </div>
      )}
      <div className="canvas-tools">
        <button aria-label="放大图谱" title="放大" onClick={() => zoom(1.25)}>
          ＋
        </button>
        <button aria-label="缩小图谱" title="缩小" onClick={() => zoom(0.8)}>
          −
        </button>
        <button
          onClick={() => {
            if (cyRef.current) fitGraph(cyRef.current);
          }}
        >
          适应画布
        </button>
      </div>
      <div className="canvas-hint">
        拖动节点调整位置 · 滚轮缩放 · 点击连线查看证据
      </div>
    </div>
  );
}
