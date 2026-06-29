import { useEffect, useMemo, useState } from "react";
import {
  CostResp,
  getCost,
  getTask,
  postCommand,
  postDecision,
  Verdict,
} from "./api/client";
import { ChatPanel } from "./components/ChatPanel";
import { CostPanel } from "./components/CostPanel";
import { DecisionModal } from "./components/DecisionModal";
import { EventTimeline } from "./components/EventTimeline";
import { TodoView } from "./components/TodoView";
import { useEventStream } from "./hooks/useEventStream";

const SESSION_ID = `web-${Math.random().toString(36).slice(2, 10)}`;

export default function App() {
  const [taskId, setTaskId] = useState<string | null>(null);
  const [cost, setCost] = useState<CostResp | null>(null);
  const [todoPlan, setTodoPlan] = useState<Array<Record<string, unknown>>>([]);
  const { events, done, error } = useEventStream(taskId);

  // 当前是否在等待人在环决策(最近一条是 need_decision 且其后没有 decision)
  const pendingDecision = useMemo(() => {
    let pending: (typeof events)[number] | null = null;
    for (const e of events) {
      if (e.event === "need_decision") pending = e;
      if (e.event === "decision" || e.event === "done") pending = null;
    }
    return pending;
  }, [events]);

  const running = taskId !== null && !done;

  // 任务结束:拉成本与快照(TODO 真源)
  useEffect(() => {
    if (!taskId || !done) return;
    getCost(taskId).then(setCost).catch(() => undefined);
    getTask(taskId)
      .then((s) => setTodoPlan((s.todo_plan as Array<Record<string, unknown>>) || []))
      .catch(() => undefined);
  }, [taskId, done]);

  // dev_plan 事件到达即异步拉一次快照,让 TODO 早点出现
  useEffect(() => {
    if (taskId && events.some((e) => e.event === "dev_plan") && todoPlan.length === 0) {
      getTask(taskId)
        .then((s) => setTodoPlan((s.todo_plan as Array<Record<string, unknown>>) || []))
        .catch(() => undefined);
    }
  }, [events, taskId, todoPlan.length]);

  const onSubmit = async (text: string) => {
    setCost(null);
    setTodoPlan([]);
    const resp = await postCommand(text, SESSION_ID);
    setTaskId(resp.task_id);
  };

  const onDecide = async (verdict: Verdict) => {
    if (!taskId) return;
    await postDecision(taskId, verdict);
  };

  return (
    <div className="app">
      <header className="topbar">
        <h1>一人公司 · OPC Studio</h1>
        <div className="status">
          {taskId && (
            <span className={`badge ${done ? "ok" : "run"}`}>
              {error ? "异常" : done ? "已收口" : "运行中"} · {taskId}
            </span>
          )}
        </div>
      </header>

      <div className="grid">
        <div className="col-left">
          <ChatPanel onSubmit={onSubmit} disabled={running} />
          <TodoView events={events} todoPlan={todoPlan} />
          <CostPanel cost={cost} />
        </div>
        <div className="col-right">
          <EventTimeline events={events} />
        </div>
      </div>

      {pendingDecision && <DecisionModal event={pendingDecision} onDecide={onDecide} />}
    </div>
  );
}
