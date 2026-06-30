import { useEffect, useRef, useState } from "react";
import { eventsUrl, OpcEvent } from "../api/client";

export interface EventStreamState {
  events: OpcEvent[];
  done: boolean;
  error: string | null;
}

// 订阅某个 task 的 SSE 事件流。done/error 后服务端收流,EventSource 自动结束。
export function useEventStream(taskId: string | null): EventStreamState {
  const [events, setEvents] = useState<OpcEvent[]>([]);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!taskId) return;
    setEvents([]);
    setDone(false);
    setError(null);

    const es = new EventSource(eventsUrl(taskId));
    esRef.current = es;

    const handle = (e: MessageEvent) => {
      try {
        const data: OpcEvent = JSON.parse(e.data);
        setEvents((prev) => [...prev, data]);
        if (data.event === "done") {
          setDone(true);
          es.close();
        } else if (data.event === "error") {
          setError(String(data.payload?.error ?? "任务异常"));
          setDone(true);
          es.close();
        }
      } catch {
        /* 忽略 ping 等非 JSON 帧 */
      }
    };

    // 后端用具名事件(event: <type>),用 addEventListener 逐类型挂载。
    const TYPES = [
      "graph_start", "role_start", "thinking", "tool_call", "artifact",
      "handoff", "rework", "need_decision", "decision", "error", "done",
      "ceo_route", "dev_plan", "build", "rule_check", "loop_judge",
      "acceptance", "cost_soft_limit", "node_retry", "message",
      // M6 Edit 链路里程碑 + 自重启信号
      "edit_start", "edit_locate", "edit_change", "edit_regression",
      "edit_review", "edit_rework", "edit_revert", "restart_required",
    ];
    TYPES.forEach((t) => es.addEventListener(t, handle as EventListener));

    es.onerror = () => {
      // 任务已 done 时 close 触发的 onerror 属正常收流,不报错。
      if (!done) setError((prev) => prev);
    };

    return () => {
      TYPES.forEach((t) => es.removeEventListener(t, handle as EventListener));
      es.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  return { events, done, error };
}
