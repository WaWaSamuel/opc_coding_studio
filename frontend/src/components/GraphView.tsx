import { useCallback, useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  Edge,
  Handle,
  MarkerType,
  Node,
  NodeProps,
  Position,
  ReactFlowProvider,
} from "reactflow";
import "reactflow/dist/style.css";
import {
  EditGraphResp,
  EditNode,
  getEditGraph,
  getRole,
  RoleDetail,
} from "../api/client";

// 节点种类 → 配色/形态(role 可下钻,gate 闸门,decision 人在环,terminal 终态)。
const KIND_CLASS: Record<string, string> = {
  entry: "rf-entry",
  role: "rf-role",
  gate: "rf-gate",
  decision: "rf-decision",
  terminal: "rf-terminal",
  fallback: "rf-fallback",
};

interface RfData {
  node: EditNode;
  onPick: (roleId: string) => void;
}

// 自定义节点:role/gate 带 role_id 可点击下钻;changed 高亮(diff)。
function OpcNode({ data }: NodeProps<RfData>) {
  const { node, onPick } = data;
  const clickable = Boolean(node.role_id);
  return (
    <div
      className={`rf-node ${KIND_CLASS[node.kind] ?? "rf-role"} ${
        node.changed ? "rf-changed" : ""
      } ${clickable ? "rf-clickable" : ""}`}
      onClick={() => node.role_id && onPick(node.role_id)}
      title={clickable ? "点击查看角色详情" : node.id}
    >
      <Handle type="target" position={Position.Top} />
      <div className="rf-node-label">{node.label}</div>
      <div className="rf-node-id">{node.role_id ?? node.id}</div>
      {node.changed && <span className="rf-badge">diff</span>}
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const NODE_TYPES = { opc: OpcNode };

// 把后端 {nodes,edges} 排成竖直 DAG(无第三方布局库,按拓扑层级竖排)。
function layout(
  graph: EditGraphResp,
  onPick: (roleId: string) => void,
): { nodes: Node<RfData>[]; edges: Edge[] } {
  const order = new Map<string, number>();
  graph.nodes.forEach((n, i) => order.set(n.id, i));
  const nodes: Node<RfData>[] = graph.nodes.map((n, i) => ({
    id: n.id,
    type: "opc",
    position: { x: 0, y: i * 110 },
    data: { node: n, onPick },
    draggable: true,
  }));
  const edges: Edge[] = graph.edges.map((e, i) => {
    // 回退/异常边(目标在源之前)画成红色虚线弯曲,正向边实线。
    const back = (order.get(e.to) ?? 0) <= (order.get(e.from) ?? 0);
    return {
      id: `e${i}`,
      source: e.from,
      target: e.to,
      label: e.label || undefined,
      type: back ? "smoothstep" : "default",
      animated: back,
      style: back ? { stroke: "#e06c9f", strokeDasharray: "4 3" } : undefined,
      markerEnd: { type: MarkerType.ArrowClosed },
    };
  });
  return { nodes, edges };
}

function RoleInspector({
  role,
  loading,
  onClose,
}: {
  role: RoleDetail | null;
  loading: boolean;
  onClose: () => void;
}) {
  return (
    <div className="role-inspector">
      <div className="ri-head">
        <b>角色详情</b>
        <button className="ri-close" onClick={onClose}>
          ×
        </button>
      </div>
      {loading && <div className="empty">加载中…</div>}
      {!loading && role && (
        <div className="ri-body">
          <div className="ri-row">
            <span className="ri-key">role_id</span>
            <span className="ri-val">{role.role_id}</span>
          </div>
          <div className="ri-row">
            <span className="ri-key">model_tier</span>
            <span className="ri-val">{role.model_tier}</span>
          </div>
          <div className="ri-block">
            <div className="ri-key">职责</div>
            <pre className="ri-resp">{role.responsibility}</pre>
          </div>
          <div className="ri-block">
            <div className="ri-key">可调 Tool</div>
            <div className="ri-tags">
              {role.tools.length === 0 && <span className="ri-muted">无</span>}
              {role.tools.map((t) => (
                <span key={t} className="ri-tag">
                  {t}
                </span>
              ))}
            </div>
          </div>
          <div className="ri-block">
            <div className="ri-key">输出字段</div>
            <div className="ri-tags">
              {role.output_schema_keys.map((k) => (
                <span key={k} className="ri-tag ghost">
                  {k}
                </span>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function GraphViewInner() {
  const [ref, setRef] = useState("main");
  const [workflow, setWorkflow] = useState("edit");
  const [graph, setGraph] = useState<EditGraphResp | null>(null);
  const [err, setErr] = useState("");
  const [role, setRole] = useState<RoleDetail | null>(null);
  const [roleLoading, setRoleLoading] = useState(false);

  const onPick = useCallback((roleId: string) => {
    setRoleLoading(true);
    setRole(null);
    getRole(roleId)
      .then(setRole)
      .catch(() => setRole(null))
      .finally(() => setRoleLoading(false));
  }, []);

  useEffect(() => {
    setErr("");
    getEditGraph(ref, workflow)
      .then(setGraph)
      .catch((e) => setErr(String(e)));
  }, [ref, workflow]);

  const { nodes, edges } = useMemo(
    () => (graph ? layout(graph, onPick) : { nodes: [], edges: [] }),
    [graph, onPick],
  );

  const workflows = graph?.workflows ?? [
    { id: "edit", label: "Edit 改系统" },
    { id: "runtime", label: "Runtime 跑业务" },
  ];

  return (
    <div className="panel graph">
      <div className="panel-title">
        工作流全景
        <span className="graph-refs">
          {workflows.map((w) => (
            <button
              key={w.id}
              className={`chip ${workflow === w.id ? "active" : ""}`}
              onClick={() => setWorkflow(w.id)}
            >
              {w.label}
            </button>
          ))}
        </span>
      </div>

      <div className="graph-meta">
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
        {graph?.git && (
          <>
            <span className={graph.git.enabled ? "on" : "off"}>
              git {graph.git.enabled ? "已启用" : "dry-run"}
            </span>
            <span className={graph.git.can_push ? "on" : "off"}>
              {graph.git.can_push ? "可推远端" : "受控不推"}
            </span>
          </>
        )}
      </div>

      {err && <div className="empty">加载失败：{err}</div>}

      <div className="rf-wrap">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          fitView
          fitViewOptions={{ padding: 0.2 }}
          proOptions={{ hideAttribution: true }}
          nodesConnectable={false}
          edgesFocusable={false}
        >
          <Background gap={16} />
          <Controls showInteractive={false} />
        </ReactFlow>
        {(role || roleLoading) && (
          <RoleInspector
            role={role}
            loading={roleLoading}
            onClose={() => setRole(null)}
          />
        )}
      </div>
    </div>
  );
}

// Edit/Runtime 工作流全景可视化(F-A.8 / M12):reactflow 渲染全工作流 + 全角色,
// role/gate 节点可点击下钻 RoleInspector;feature ref 标 changed 做 diff 高亮。
export function GraphView() {
  return (
    <ReactFlowProvider>
      <GraphViewInner />
    </ReactFlowProvider>
  );
}
