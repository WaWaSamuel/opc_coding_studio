import { useEffect, useState } from "react";
import { EditGraphResp, getEditGraph } from "../api/client";

// Edit 工作流只读可视化(F-A.8 / M12)。轻量自绘:节点按声明顺序竖排,
// 边在节点间用箭头标注语义(劣化→回退 / 异常 等);feature ref 时 changed 节点高亮。
// 真源仍是后端 GET /edit/graph,这里只渲染。
export function GraphView() {
  const [ref, setRef] = useState("main");
  const [graph, setGraph] = useState<EditGraphResp | null>(null);
  const [err, setErr] = useState<string>("");

  const load = (r: string) => {
    setErr("");
    getEditGraph(r)
      .then(setGraph)
      .catch((e) => setErr(String(e)));
  };

  useEffect(() => {
    load(ref);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ref]);

  // 从节点出边里取语义标签(竖排时画在节点下方)
  const outLabel = (id: string): string => {
    if (!graph) return "";
    const labels = graph.edges
      .filter((e) => e.from === id && e.label)
      .map((e) => e.label as string);
    return labels.join(" / ");
  };

  return (
    <div className="panel graph">
      <div className="panel-title">
        Edit 工作流
        <span className="graph-refs">
          {["main", "feature"].map((r) => (
            <button
              key={r}
              className={`chip ${ref === r ? "active" : ""}`}
              onClick={() => setRef(r)}
            >
              {r}
            </button>
          ))}
        </span>
      </div>

      {graph?.git && (
        <div className="graph-meta">
          <span>main: {graph.git.main_branch}</span>
          <span className={graph.git.enabled ? "on" : "off"}>
            git {graph.git.enabled ? "已启用" : "dry-run"}
          </span>
          <span className={graph.git.can_push ? "on" : "off"}>
            {graph.git.can_push ? "可推远端" : "受控不推"}
          </span>
        </div>
      )}

      {err && <div className="empty">加载失败:{err}</div>}

      <div className="graph-flow">
        {graph?.nodes.map((n, i) => (
          <div key={n.id} className="graph-step">
            <div className={`graph-node kind-${n.kind} ${n.changed ? "changed" : ""}`}>
              <span className="graph-node-label">{n.label}</span>
              <span className="graph-node-id">{n.id}</span>
            </div>
            {i < graph.nodes.length - 1 && (
              <div className="graph-arrow">
                <span className="graph-arrow-line">↓</span>
                {outLabel(n.id) && (
                  <span className="graph-arrow-label">{outLabel(n.id)}</span>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
