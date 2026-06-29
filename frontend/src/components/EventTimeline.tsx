import { OpcEvent } from "../api/client";

const LABEL: Record<string, string> = {
  graph_start: "受理需求",
  ceo_route: "CEO 路由分流",
  handoff: "棒次流转",
  dev_plan: "部长拆解 TODO",
  role_start: "角色启动",
  thinking: "推理中",
  tool_call: "工具调用",
  build: "执行交付",
  artifact: "产出交付物",
  rule_check: "硬约束校验",
  loop_judge: "质量判定",
  rework: "业务回退重做",
  acceptance: "需求验收",
  need_decision: "等待宿主决策",
  decision: "宿主已拍板",
  cost_soft_limit: "成本预警",
  node_retry: "节点重试",
  done: "收口完成",
  error: "异常中止",
};

function note(e: OpcEvent): string {
  const p = e.payload || {};
  if ("verdict" in p) return `判定：${String(p.verdict)}`;
  if ("department" in p) return `部门：${String(p.department)}`;
  if ("status" in p) return `状态：${String(p.status)}`;
  if ("note" in p) return String(p.note).slice(0, 80);
  if ("error" in p) return String(p.error).slice(0, 100);
  if ("todo_items" in p) return `TODO ${String(p.todo_items)} 条`;
  return e.role || "";
}

export function EventTimeline({ events }: { events: OpcEvent[] }) {
  return (
    <div className="panel timeline">
      <div className="panel-title">流转留痕 ({events.length})</div>
      <div className="timeline-body">
        {events.length === 0 && <div className="empty">暂无事件，发送指令后实时显示。</div>}
        {events.map((e, i) => (
          <div className={`tl-item ev-${e.event}`} key={i}>
            <span className="tl-dot" />
            <div className="tl-content">
              <div className="tl-head">
                <b>{LABEL[e.event] ?? e.event}</b>
                {e.role && <span className="tl-role">{e.role}</span>}
              </div>
              <div className="tl-note">{note(e)}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
