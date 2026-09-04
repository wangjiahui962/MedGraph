import { useEffect, useRef, useState } from "react";
import cytoscape, { type Core, type NodeSingular } from "cytoscape";
import fcose from "cytoscape-fcose";
import type { GraphMode, GraphView } from "./graph";

cytoscape.use(fcose);

const TYPE_COLORS: Record<string, string> = {
  Disease: "#17796f",
  Symptom: "#d28a2f",
  Drug: "#557bc0",
  Treatment: "#9b67a0",
  Examination: "#65884f",
  Complication: "#c25f61",
  RiskFactor: "#9b7447",
  Department: "#557f8e",
  Population: "#8a6db2",
  未分类: "#899a9e",
};

const TYPE_SHAPES: Record<string, cytoscape.Css.NodeShape> = {
  Disease: "ellipse",
  Symptom: "round-rectangle",
  Drug: "hexagon",
  Treatment: "diamond",
  Examination: "rectangle",
  Complication: "octagon",
  RiskFactor: "triangle",
  Department: "tag",
  Population: "pentagon",
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

export const palette = Object.values(TYPE_COLORS);
export const colorForType = (type: string, fallbackIndex = 0) =>
  TYPE_COLORS[type] ?? palette[fallbackIndex % palette.length];
export const shapeForType = (type: string) =>
  TYPE_SHAPES[type] ?? "ellipse";

function fitGraph(cy: Core) {
  cy.fit(undefined, 54);
  if (cy.zoom() > 1) {
    cy.zoom(1);
    cy.center();
  }
}

/**
 * fCoSE 未配合 layout-utilities 时不会自动打包断开的连通分量。
 * 将每个分量按面积降序装入多行，保证它们的包围盒互不重叠。
 */
function packDisconnectedComponents(cy: Core, spacing = 72) {
  const components = cy
    .elements()
    .components()
    .map((component) => {
      const nodes = component.nodes();
      const box = nodes.boundingBox({
        includeLabels: false,
        includeOverlays: false,
      });
      return {
        nodes,
        box,
        width: Math.max(box.w, 32),
        height: Math.max(box.h, 32),
      };
    })
    .sort(
      (a, b) =>
        b.width * b.height - a.width * a.height ||
        b.nodes.length - a.nodes.length,
    );
  if (components.length < 2) return;

  const totalArea = components.reduce(
    (sum, component) =>
      sum +
      (component.width + spacing) * (component.height + spacing),
    0,
  );
  const targetWidth = Math.max(720, Math.sqrt(totalArea) * 1.35);
  let cursorX = 0;
  let cursorY = 0;
  let rowHeight = 0;

  cy.batch(() => {
    for (const component of components) {
      if (
        cursorX > 0 &&
        cursorX + component.width > targetWidth
      ) {
        cursorX = 0;
        cursorY += rowHeight + spacing;
        rowHeight = 0;
      }
      const offsetX = cursorX - component.box.x1;
      const offsetY = cursorY - component.box.y1;
      component.nodes.positions((node) => ({
        x: node.position("x") + offsetX,
        y: node.position("y") + offsetY,
      }));
      cursorX += component.width + spacing;
      rowHeight = Math.max(rowHeight, component.height);
    }
  });
}

function isVisible(node: NodeSingular, cy: Core) {
  const position = node.renderedPosition();
  return (
    position.x >= -30 &&
    position.y >= -30 &&
    position.x <= cy.width() + 30 &&
    position.y <= cy.height() + 30
  );
}

export function GraphCanvas({
  view,
  types,
  selectedEdge,
  onNode,
  onEdge,
  mode,
}: {
  view: GraphView;
  types: string[];
  selectedEdge: string | null;
  onNode: (id: string) => void;
  onEdge: (id: string | null) => void;
  mode: GraphMode;
}) {
  const container = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const handlers = useRef({ onNode, onEdge });
  const [layoutBusy, setLayoutBusy] = useState(false);
  const [hovered, setHovered] = useState<{
    text: string;
    x: number;
    y: number;
  } | null>(null);
  handlers.current = { onNode, onEdge };

  useEffect(() => {
    if (!container.current || !view.nodes.length) {
      setLayoutBusy(false);
      return;
    }
    const degree = new Map(view.nodes.map((node) => [node.id, 0]));
    for (const edge of view.edges) {
      degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
      if (edge.source !== edge.target)
        degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
    }
    const nodeById = new Map(view.nodes.map((node) => [node.id, node]));
    const diseaseNodes = view.nodes.filter((node) => node.type === "Disease");
    const symptomNodes = view.nodes.filter((node) => node.type === "Symptom");
    const bipartiteRank = new Map<string, number>();
    diseaseNodes.forEach((node, index) => bipartiteRank.set(node.id, index));
    symptomNodes.forEach((node, index) => bipartiteRank.set(node.id, index));

    const cy = cytoscape({
      container: container.current,
      pixelRatio: Math.min(window.devicePixelRatio || 1, 1.75),
      textureOnViewport: true,
      hideEdgesOnViewport: view.edges.length > 1800,
      motionBlur: false,
      elements: [
        ...view.nodes.map((node, index) => {
          const nodeDegree = degree.get(node.id) ?? 0;
          const baseSize = Math.min(
            34,
            10 + Math.sqrt(nodeDegree) * 3 + (node.id === view.focusId ? 8 : 0),
          );
          let position = {
            x:
              Math.cos((index * 2 * Math.PI) / Math.max(view.nodes.length, 1)) *
              260,
            y:
              Math.sin((index * 2 * Math.PI) / Math.max(view.nodes.length, 1)) *
              260,
          };
          if (mode === "bipartite") {
            const left = node.type === "Disease";
            const group = left ? diseaseNodes : symptomNodes;
            const rank = bipartiteRank.get(node.id) ?? 0;
            position = {
              x: left ? 0 : 760,
              y: (rank - (group.length - 1) / 2) * 38,
            };
          }
          return {
            data: {
              id: node.id,
              label: node.name,
              color: colorForType(node.type, types.indexOf(node.type)),
              shape: shapeForType(node.type),
              focus: node.id === view.focusId ? 1 : 0,
              degree: nodeDegree,
              baseSize,
              renderSize: baseSize,
              renderFontSize: 12,
              renderTextWidth: 110,
              renderTextMargin: 5,
              renderOutlineWidth: 2,
              renderBorderWidth: 1,
              focusBorderWidth: node.id === view.focusId ? 3 : 1,
              neighborBorderWidth: 1.8,
              highlightBorderWidth: 3,
              isolated: nodeDegree === 0 ? 1 : 0,
            },
            position,
          };
        }),
        ...view.edges.map((edge) => ({
          data: {
            id: edge.id,
            source: edge.source,
            target: edge.target,
            label: RELATION_LABELS[edge.relation] ?? edge.relation,
            confidence: edge.confidence,
            directed:
              edge.relation === "RELATED_TO" ||
              edge.relation === "SHARES_SYMPTOM"
                ? 0
                : 1,
            derived: edge.derived ? 1 : 0,
            baseEdgeWidth: edge.derived
              ? Math.min(7, 1.2 + Math.log2(edge.weight ?? 1) * 1.35)
              : 1.25,
            renderEdgeWidth: edge.derived
              ? Math.min(7, 1.2 + Math.log2(edge.weight ?? 1) * 1.35)
              : 1.25,
            selectedEdgeWidth: 3.4,
            renderFontSize: 11,
            renderTextPadding: 4,
            baseArrowScale: mode === "focus" ? 0.62 : 0.42,
            renderArrowScale: mode === "focus" ? 0.62 : 0.42,
            highlightEdgeWidth: edge.derived
              ? Math.min(9, 2 + Math.log2(edge.weight ?? 1) * 1.6)
              : 2.4,
          },
        })),
      ],
      style: [
        {
          selector: "node",
          style: {
            "background-color": "data(color)",
            shape: "data(shape)" as cytoscape.Css.NodeShape,
            label: "",
            color: "#263d43",
            "font-size": "data(renderFontSize)",
            "font-family":
              "Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif",
            "text-valign": "bottom",
            "text-margin-y": "data(renderTextMargin)" as unknown as number,
            "text-wrap": "ellipsis",
            "text-max-width": "data(renderTextWidth)",
            "text-outline-color": "#ffffff",
            "text-outline-width": "data(renderOutlineWidth)",
            width: "data(renderSize)",
            height: "data(renderSize)",
            "border-width": "data(renderBorderWidth)",
            "border-color": "#ffffff",
            opacity: 0.95,
          },
        },
        { selector: "node.show-label", style: { label: "data(label)" } },
        { selector: "node[isolated = 1]", style: { opacity: 0.35 } },
        {
          selector: "node[focus = 1]",
          style: {
            "border-width": "data(focusBorderWidth)",
            "border-color": "#b8ded6",
            "font-weight": "bold",
            "z-index": 10,
          },
        },
        {
          selector: "edge",
          style: {
            width: "data(renderEdgeWidth)",
            "line-color": "#7653b6",
            "target-arrow-color": "#63429f",
            "target-arrow-shape": "triangle",
            "arrow-scale": "data(renderArrowScale)" as unknown as number,
            "curve-style": "bezier",
            label: "",
            opacity: 0.68,
          },
        },
        {
          selector: "edge[directed = 0]",
          style: { "target-arrow-shape": "none" },
        },
        {
          selector: "edge[confidence < 0.6]",
          style: {
            "line-color": "#d58b2c",
            "target-arrow-color": "#d58b2c",
            "line-style": "dashed",
            opacity: 0.38,
          },
        },
        {
          selector: "edge[confidence >= 0.8]",
          style: {
            "line-color": "#1b806f",
            "target-arrow-color": "#1b806f",
          },
        },
        {
          selector: "edge[derived = 1]",
          style: {
            "line-color": "#3f8b9c",
            "target-arrow-shape": "none",
            opacity: 0.58,
          },
        },
        {
          selector: "edge:selected",
          style: {
            width: "data(selectedEdgeWidth)",
            "line-color": "#114f75",
            "target-arrow-color": "#114f75",
            color: "#183f4c",
            label: "data(label)",
            "font-size": "data(renderFontSize)",
            "text-background-color": "#ffffff",
            "text-background-opacity": 0.92,
            "text-background-padding": "data(renderTextPadding)",
            "z-index": 20,
          },
        },
        {
          selector: "node.is-dimmed",
          style: { opacity: 0.07, label: "" },
        },
        {
          selector: "edge.is-dimmed",
          style: { opacity: 0.025 },
        },
        {
          selector: "node.is-neighbor",
          style: {
            opacity: 1,
            "border-width": "data(neighborBorderWidth)",
            "border-color": "#7db9ad",
            "z-index": 30,
          },
        },
        {
          selector: "node.is-highlighted",
          style: {
            opacity: 1,
            "border-width": "data(highlightBorderWidth)",
            "border-color": "#123f45",
            "z-index": 40,
          },
        },
        {
          selector: "node.force-label",
          style: { label: "data(label)" },
        },
        {
          selector: "edge.is-neighborhood",
          style: {
            opacity: 0.96,
            width: "data(highlightEdgeWidth)",
            "z-index": 35,
          },
        },
      ],
      layout: { name: "preset" },
      minZoom: 0.06,
      maxZoom: 5,
      wheelSensitivity: 0.16,
    });
    cyRef.current = cy;

    let frame = 0;
    const updateSemanticZoom = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const zoom = Math.max(cy.zoom(), 0.06);
        const inverseZoom = 1 / zoom;
        for (const node of cy.nodes()) {
          const size = Math.max(
            4,
            Math.min(110, node.data("baseSize") / Math.pow(zoom, 0.92)),
          );
          node.data("renderSize", size);
          // Cytoscape 的字号使用模型坐标；除以缩放倍率后，屏幕字号保持稳定。
          node.data(
            "renderFontSize",
            Math.max(2.2, Math.min(48, 12 * inverseZoom)),
          );
          node.data(
            "renderTextWidth",
            Math.max(24, Math.min(280, 120 * inverseZoom)),
          );
          node.data(
            "renderTextMargin",
            Math.max(1, Math.min(24, 6 * inverseZoom)),
          );
          node.data(
            "renderOutlineWidth",
            Math.max(0.35, Math.min(7, 1.8 * inverseZoom)),
          );
          node.data(
            "renderBorderWidth",
            Math.max(0.18, Math.min(4, inverseZoom)),
          );
          node.data(
            "focusBorderWidth",
            Math.max(0.55, Math.min(10, 3 * inverseZoom)),
          );
          node.data(
            "neighborBorderWidth",
            Math.max(0.35, Math.min(7, 1.8 * inverseZoom)),
          );
          node.data(
            "highlightBorderWidth",
            Math.max(0.55, Math.min(10, 3 * inverseZoom)),
          );
          node.removeClass("show-label");
        }
        for (const edge of cy.edges()) {
          const scale = 1 / Math.pow(zoom, 0.82);
          edge.data(
            "renderEdgeWidth",
            Math.max(0.24, Math.min(14, Number(edge.data("baseEdgeWidth")) * scale)),
          );
          edge.data(
            "selectedEdgeWidth",
            Math.max(0.8, Math.min(18, 3.4 * scale)),
          );
          edge.data(
            "renderFontSize",
            Math.max(2, Math.min(44, 11 * inverseZoom)),
          );
          edge.data(
            "renderTextPadding",
            Math.max(0.7, Math.min(16, 4 * inverseZoom)),
          );
          edge.data(
            "renderArrowScale",
            Math.max(
              0.1,
              Math.min(
                0.8,
                Number(edge.data("baseArrowScale")) / Math.pow(zoom, 0.8),
              ),
            ),
          );
          edge.data(
            "highlightEdgeWidth",
            Math.max(
              0.6,
              Math.min(18, Number(edge.data("baseEdgeWidth")) * 1.9 * scale),
            ),
          );
        }
        const screenCapacity = Math.max(
          12,
          Math.min(90, Math.floor((cy.width() * cy.height()) / 7200)),
        );
        const labelLimit =
          zoom < 0.2
            ? 10
            : zoom < 0.42
              ? Math.min(22, screenCapacity)
              : zoom < 0.8
                ? Math.min(42, screenCapacity)
                : screenCapacity;
        const candidates = cy
          .nodes()
          .filter((node) => isVisible(node, cy))
          .sort(
            (a, b) =>
              Number(b.data("focus")) - Number(a.data("focus")) ||
              Number(b.data("degree")) - Number(a.data("degree")) ||
              String(a.data("label")).localeCompare(
                String(b.data("label")),
                "zh-CN",
              ),
          );
        const occupied: Array<{
          left: number;
          right: number;
          top: number;
          bottom: number;
        }> = [];
        let shown = 0;
        candidates.forEach((node) => {
          if (shown >= labelLimit) return;
          const position = node.renderedPosition();
          const label = String(node.data("label"));
          const textWidth = Math.min(
            156,
            Math.max(38, Array.from(label).length * 12),
          );
          const screenNodeSize = Number(node.data("renderSize")) * zoom;
          const top = position.y + screenNodeSize / 2 + 5;
          const box = {
            left: position.x - textWidth / 2 - 5,
            right: position.x + textWidth / 2 + 5,
            top,
            bottom: top + 22,
          };
          const collides = occupied.some(
            (other) =>
              box.left < other.right &&
              box.right > other.left &&
              box.top < other.bottom &&
              box.bottom > other.top,
          );
          if (collides && !Number(node.data("focus"))) return;
          node.addClass("show-label");
          occupied.push(box);
          shown++;
        });
      });
    };

    const layoutOptions =
      mode === "bipartite"
        ? ({ name: "preset", padding: 64 } as cytoscape.LayoutOptions)
        : ({
              name: "fcose",
              quality:
                mode === "focus" || mode === "confidence"
                  ? "default"
                  : "draft",
              animate: false,
              randomize: mode !== "focus",
              nodeDimensionsIncludeLabels: false,
              idealEdgeLength:
                mode === "similarity"
                  ? 115
                  : mode === "confidence"
                    ? 104
                    : mode === "full"
                      ? 62
                      : 88,
              nodeRepulsion:
                mode === "similarity"
                  ? 12500
                  : mode === "confidence"
                    ? 14000
                    : mode === "full"
                      ? 5200
                      : 8500,
              padding: 64,
            } as cytoscape.LayoutOptions);
    const layout = cy.layout(layoutOptions);
    layout.on("layoutstart", () => setLayoutBusy(true));
    layout.on("layoutstop", () => {
      if (mode === "confidence") packDisconnectedComponents(cy, 72);
      fitGraph(cy);
      updateSemanticZoom();
      if (import.meta.env.DEV && container.current && view.focusId) {
        const focusNode = cy.getElementById(view.focusId);
        if (focusNode.nonempty()) {
          const position = focusNode.renderedPosition();
          container.current.dataset.focusX = String(position.x);
          container.current.dataset.focusY = String(position.y);
        }
      }
      setLayoutBusy(false);
    });
    layout.run();

    cy.on("zoom pan", updateSemanticZoom);
    const clearNeighborhoodHighlight = () => {
      cy.elements().removeClass(
        "is-dimmed is-neighbor is-highlighted is-neighborhood force-label",
      );
      if (container.current) delete container.current.dataset.highlightedNode;
    };
    const highlightNeighborhood = (node: NodeSingular) => {
      cy.batch(() => {
        clearNeighborhoodHighlight();
        cy.elements().addClass("is-dimmed");
        const neighbors = node.neighborhood("node");
        const edges = node.connectedEdges();
        node.removeClass("is-dimmed").addClass("is-highlighted force-label");
        neighbors
          .removeClass("is-dimmed")
          .addClass("is-neighbor force-label");
        edges
          .removeClass("is-dimmed")
          .addClass("is-neighborhood");
      });
      handlers.current.onEdge(null);
      if (container.current)
        container.current.dataset.highlightedNode = node.id();
    };
    cy.on("tap", "node", (event) => highlightNeighborhood(event.target));
    cy.on("dbltap", "node", (event) => {
      if (container.current)
        container.current.dataset.navigatedNode = event.target.id();
      handlers.current.onNode(event.target.id());
    });
    cy.on("tap", (event) => {
      if (event.target === cy) {
        clearNeighborhoodHighlight();
        handlers.current.onEdge(null);
      }
    });
    cy.on("tap", "edge", (event) => handlers.current.onEdge(event.target.id()));
    cy.on("mouseover", "node", (event) => {
      const position = event.renderedPosition;
      setHovered({
        text: event.target.data("label"),
        x: position.x + 12,
        y: position.y + 12,
      });
    });
    cy.on("mouseover", "edge", (event) => {
      const edge = event.target;
      const position = event.renderedPosition;
      const source = nodeById.get(edge.data("source"))?.name ?? "";
      const target = nodeById.get(edge.data("target"))?.name ?? "";
      const relation = edge.data("label");
      const connector = edge.data("directed") ? "→" : "—";
      setHovered({
        text: `${source} ${connector} ${relation} ${connector} ${target}`,
        x: position.x + 12,
        y: position.y + 12,
      });
    });
    cy.on("mouseout", "node, edge", () => setHovered(null));
    const observer = new ResizeObserver(() => {
      cy.resize();
      updateSemanticZoom();
    });
    observer.observe(container.current);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      cy.destroy();
      cyRef.current = null;
    };
  }, [view, types, mode]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.edges().unselect();
    if (selectedEdge) cy.getElementById(selectedEdge).select();
  }, [selectedEdge, view]);

  function zoom(factor: number) {
    const cy = cyRef.current;
    if (!cy) return;
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
        aria-label="实体关系图。单击节点高亮一跳关系，双击节点进入实体聚焦。"
      />
      {hovered && (
        <div
          className="node-tooltip"
          style={{ left: hovered.x, top: hovered.y }}
        >
          {hovered.text}
        </div>
      )}
      {!view.nodes.length && (
        <div className="canvas-empty">
          当前模式下没有可展示实体
          <br />
          <small>请调整筛选条件、阈值或视图模式</small>
        </div>
      )}
      {layoutBusy && (
        <div className="layout-status" role="status">
          <i /> 正在排列 {view.nodes.length.toLocaleString()} 个实体…
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
          aria-label="适应画布"
          onClick={() => cyRef.current && fitGraph(cyRef.current)}
        >
          适应画布
        </button>
      </div>
      <div className="canvas-hint">
        拖动调整 · 滚轮缩放 · 单击节点高亮一跳 · 双击节点进入聚焦
      </div>
    </div>
  );
}
