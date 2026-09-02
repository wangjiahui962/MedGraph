import { useEffect, useRef } from "react";
import cytoscape, { type Core } from "cytoscape";
import type { GraphView } from "./graph";

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
    const cy = cytoscape({
      container: container.current,
      elements: [
        ...view.nodes.map((n, index) => ({
          data: {
            id: n.id,
            label: n.name,
            color: palette[types.indexOf(n.type) % palette.length],
            focus: n.id === view.focusId ? 1 : 0,
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
            "font-size": 12,
            "font-family": "Microsoft YaHei, sans-serif",
            "text-valign": "bottom",
            "text-margin-y": 9,
            "text-wrap": "ellipsis",
            "text-max-width": "125px",
            width: 26,
            height: 26,
            "border-width": 5,
            "border-color": "#ffffff",
          },
        },
        {
          selector: "node[focus = 1]",
          style: {
            width: 46,
            height: 46,
            "border-width": 7,
            "border-color": "#d9ebe8",
            "font-weight": "bold",
            "font-size": 14,
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
            label: "data(label)",
            "font-size": 10,
            color: "#6c8389",
            "text-background-color": "#f7faf9",
            "text-background-opacity": 0.92,
            "text-background-padding": "3px",
            "text-rotation": "autorotate",
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
      layout:
        view.nodes.length <= 12
          ? {
              name: "concentric",
              concentric: (node) => (node.data("focus") ? 10 : 1),
              levelWidth: () => 1,
              minNodeSpacing: 90,
              padding: 48,
              animate: false,
            }
          : {
              name: "cose",
              animate: false,
              randomize: false,
              nodeRepulsion: () => 16000,
              idealEdgeLength: () => 150,
              padding: 48,
            },
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
