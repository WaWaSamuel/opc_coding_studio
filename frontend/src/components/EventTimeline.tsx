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
  // M6 Edit 链路
  edit_start: "受理改系统",
  edit_locate: "部长定位 + TODO",
  edit_change: "工程师改动",
  edit_regression: "回归测试判定",
  edit_review: "变更评审提 PR",
  edit_rework: "回归回退重做",
  edit_revert: "git revert 回滚",
  restart_required: "服务重启信号",
};

// 一句话概述"这一步做了什么"(F-A.4 popover 头部)。
function summary(e: OpcEvent): string {
  const p = e.payload || {};
  if ("verdict" in p) return `判定：${String(p.verdict)}`;
  if ("targets" in p) return `定位目标 ${(p.targets as unknown[])?.length ?? 0} 处`;
  if ("scope" in p) return `需重启范围：${String(p.scope)}`;
  if ("pass_rate" in p) return `通过率 ${(Number(p.pass_rate) * 100).toFixed(0)}%`;
  if ("department" in p) return `部门：${String(p.department)}`;
  if ("status" in p) return `状态：${String(p.status)}`;
  if ("note" in p) return String(p.note);
  if ("error" in p) return String(p.error);
  if ("todo_items" in p) return `TODO ${String(p.todo_items)} 条`;
  if ("files" in p) {
    const f = p.files as unknown[];
    return `涉及文件 ${f?.length ?? 0} 个`;
  }
  return e.role || LABEL[e.event] || e.event;
}

function note(e: OpcEvent): string {
  return summary(e).slice(0, 90);
}

// F-A.4 流转留痕 hover 详情:做了什么 / 耗时 latency_ms / 消耗 tokens.in,out,合计 / ts。
// 数据全部来自既有事件信封字段(此前丢弃的 tokens/latency_ms),仅前端渲染。
function EventDetailPopover({ e }: { e: OpcEvent }) {
  const tin = e.tokens?.in ?? 0;
  const tout = e.tokens?.out ?? 0;
  const total = tin + tout;
  const lat = e.latency_ms ?? 0;
  const ts = e.ts ? new Date(e.ts).toLocaleTimeString() : "—";
  return (
    <div className="ev-pop">
      <div className="pop-title">{LABEL[e.event] ?? e.event}</div>
      <div className="pop-did">做了什么：{summary(e)}</div>
      <div className="pop-row">
        <span>耗时</span>
        <b>{lat} ms</b>
      </div>
      <div className="pop-row">
        <span>tokens（in / out）</span>
        <b>
          {tin} / {tout}
        </b>
      </div>
      <div className="pop-row">
        <span>tokens 合计</span>
        <b>{total}</b>
      </div>
      <div className="pop-row">
        <span>时间</span>
        <b>{ts}</b>
      </div>
    </div>
  );
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
            <EventDetailPopover e={e} />
          </div>
        ))}
      </div>
    </div>
  );
}
