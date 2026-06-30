import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChatAttachment,
  ChatMessage,
  CostResp,
  getCost,
  getTask,
  OpcEvent,
  postCommand,
  postDecision,
  postDecisionText,
  Verdict,
} from "./api/client";
import { ChatPanel } from "./components/ChatPanel";
import { CostPanel } from "./components/CostPanel";
import { EventTimeline } from "./components/EventTimeline";
import { GraphView } from "./components/GraphView";
import { HistoryPanel } from "./components/HistoryPanel";
import { TodoView } from "./components/TodoView";
import { useEventStream } from "./hooks/useEventStream";

// F-A.12 持久 SESSION_ID:同一浏览器跨刷新沿用同一会话,使后续消息能续跑到
// 同 session 的活跃任务,而非每次刷新都另起会话丢失上下文。
function persistentSessionId(): string {
  const KEY = "opc.session_id";
  try {
    const saved = localStorage.getItem(KEY);
    if (saved) return saved;
    const fresh = `web-${Math.random().toString(36).slice(2, 10)}`;
    localStorage.setItem(KEY, fresh);
    return fresh;
  } catch {
    return `web-${Math.random().toString(36).slice(2, 10)}`;
  }
}

const SESSION_ID = persistentSessionId();

// F-A.11 从终态事件流里凝练一句对话式回执(结论 + 产物摘要 + 耗时/tokens)。
function buildReceipt(events: OpcEvent[]): { text: string; tone: "done" | "error" } {
  const last = events[events.length - 1];
  const totalTokens = events.reduce(
    (s, e) => s + (e.tokens?.in ?? 0) + (e.tokens?.out ?? 0),
    0,
  );
  const totalLatency = events.reduce((s, e) => s + (e.latency_ms ?? 0), 0);
  const cost = `（耗时 ${totalLatency}ms · tokens ${totalTokens}）`;
  if (last?.event === "error") {
    return { text: `任务异常中止：${String(last.payload?.error ?? "未知错误")}`, tone: "error" };
  }
  // restart_required:落在 backend/frontend 的改动需重启才生效。
  const restart = events.find((e) => e.event === "restart_required");
  const editDone = events.find((e) => e.event === "edit_review");
  if (editDone) {
    const scope = restart ? `，改动落在 ${String(restart.payload?.scope)}，需重启生效` : "";
    return {
      text: `改系统已完成并就绪：PR 已提交待确认 Merge${scope}。${cost}`,
      tone: "done",
    };
  }
  const note = String(last?.payload?.note ?? "需求已交付。");
  return { text: `已收口：${note} ${cost}`, tone: "done" };
}

export default function App() {
  const [taskId, setTaskId] = useState<string | null>(null);
  const [cost, setCost] = useState<CostResp | null>(null);
  const [todoPlan, setTodoPlan] = useState<Array<Record<string, unknown>>>([]);
  const [view, setView] = useState<"runtime" | "edit">("runtime");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const { events, done, error } = useEventStream(taskId);
  const receiptDone = useRef<string | null>(null);

  // 当前是否在等待人在环决策(最近一条是 need_decision 且其后没有 decision)
  const pendingDecision = useMemo(() => {
    let pending: OpcEvent | null = null;
    for (const e of events) {
      if (e.event === "need_decision") pending = e;
      if (e.event === "decision" || e.event === "done") pending = null;
    }
    return pending;
  }, [events]);

  const running = taskId !== null && !done;

  // 任务结束:拉成本与快照(TODO 真源)+ 落一条对话式回执气泡(F-A.11)
  useEffect(() => {
    if (!taskId || !done) return;
    getCost(taskId).then(setCost).catch(() => undefined);
    getTask(taskId)
      .then((s) => setTodoPlan((s.todo_plan as Array<Record<string, unknown>>) || []))
      .catch(() => undefined);
    if (receiptDone.current !== taskId && events.length > 0) {
      receiptDone.current = taskId;
      const { text, tone } = buildReceipt(events);
      setMessages((prev) => [
        ...prev,
        { role: "assistant", text, ts: Date.now(), tone },
      ]);
      // 任务收口 → 刷新历史会话列表(新任务进列表 / 状态更新)。
      setHistoryRefresh((n) => n + 1);
    }
  }, [taskId, done, events]);

  // dev_plan(Runtime)/ edit_locate(Edit)事件到达即异步拉一次快照,让 TODO 早点出现
  useEffect(() => {
    if (
      taskId &&
      events.some((e) => e.event === "dev_plan" || e.event === "edit_locate") &&
      todoPlan.length === 0
    ) {
      getTask(taskId)
        .then((s) => setTodoPlan((s.todo_plan as Array<Record<string, unknown>>) || []))
        .catch(() => undefined);
    }
  }, [events, taskId, todoPlan.length]);

  const onSubmit = async (text: string, attachments: ChatAttachment[]) => {
    // F-A.7 通道①:任务在等决策时,把这条对话当作自然语言决策回复优先解析。
    if (pendingDecision && taskId && !done) {
      setMessages((prev) => [
        ...prev,
        { role: "host", text, ts: Date.now(), attachments },
      ]);
      const verdict = await postDecisionText(taskId, text).catch(() => null);
      if (!verdict) {
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            text: "没读懂你的决策意图，请点上方按钮，或用更明确的措辞（通过 / 打回 / 终止）。",
            ts: Date.now(),
          },
        ]);
      }
      return;
    }

    setCost(null);
    setTodoPlan([]);
    const hostMsg: ChatMessage = { role: "host", text, ts: Date.now(), attachments };
    const nextMessages = [...messages, hostMsg];
    setMessages(nextMessages);
    // F-A.9 多轮:把已有对话历史 + 本条一起透传(同 session 上下文延续)。
    const resp = await postCommand(text, SESSION_ID, view, {
      messages: nextMessages,
      attachments,
    });
    setTaskId(resp.task_id);
  };

  const onDecide = async (verdict: Verdict) => {
    if (!taskId) return;
    await postDecision(taskId, verdict);
  };

  // F-A.12 选中历史会话 → 回放该任务(SSE 端点会补发已落库事件,完成态即全程回放)。
  // 切换前清空当前任务态,receiptDone 复位避免误判已生成回执。
  const onPickHistory = (picked: string) => {
    if (picked === taskId) return;
    receiptDone.current = null;
    setCost(null);
    setTodoPlan([]);
    setMessages([]);
    setTaskId(picked);
  };

  return (
    <div className="app">
      <header className="topbar">
        <h1>一人公司 · OPC Studio</h1>
        <div className="status">
          <span className="view-tabs">
            <button
              className={`chip ${view === "runtime" ? "active" : ""}`}
              onClick={() => setView("runtime")}
            >
              Runtime 跑业务
            </button>
            <button
              className={`chip ${view === "edit" ? "active" : ""}`}
              onClick={() => setView("edit")}
            >
              Edit 改系统
            </button>
          </span>
          {taskId && (
            <span className={`badge ${done ? "ok" : "run"}`}>
              {error ? "异常" : done ? "已收口" : "运行中"} · {taskId}
            </span>
          )}
        </div>
      </header>

      {view === "edit" ? (
        <div className="grid">
          <div className="col-left">
            <ChatPanel
              messages={messages}
              onSubmit={onSubmit}
              disabled={running && !pendingDecision}
              pendingDecision={pendingDecision}
              onDecide={onDecide}
            />
            <TodoView events={events} todoPlan={todoPlan} />
            <GraphView />
          </div>
          <div className="col-right">
            <HistoryPanel
              system="edit"
              activeTaskId={taskId}
              onPick={onPickHistory}
              refreshKey={historyRefresh}
            />
            <EventTimeline events={events} />
          </div>
        </div>
      ) : (
        <div className="grid">
          <div className="col-left">
            <ChatPanel
              messages={messages}
              onSubmit={onSubmit}
              disabled={running && !pendingDecision}
              pendingDecision={pendingDecision}
              onDecide={onDecide}
            />
            <TodoView events={events} todoPlan={todoPlan} />
            <CostPanel cost={cost} />
          </div>
          <div className="col-right">
            <HistoryPanel
              system="runtime"
              activeTaskId={taskId}
              onPick={onPickHistory}
              refreshKey={historyRefresh}
            />
            <EventTimeline events={events} />
          </div>
        </div>
      )}
    </div>
  );
}
