// 统一事件信封(对齐后端 EventBus.Event / PRD 5.5)
export interface OpcEvent {
  task_id: string;
  event: string;
  role?: string;
  payload?: Record<string, unknown>;
  tokens?: { in: number; out: number };
  latency_ms?: number;
  ts?: string;
}

export interface CommandResp {
  task_id: string;
  intent: string;
}

export interface CostByRole {
  tokens: number;
  latency_ms: number;
  calls: number;
}

export interface CostResp {
  task_id: string;
  total_tokens: number;
  total_latency_ms: number;
  by_role: Record<string, CostByRole>;
}

export interface TaskSnapshot {
  task_id: string;
  status: string;
  todo_plan?: Array<Record<string, unknown>>;
  [k: string]: unknown;
}

export type Verdict = "pass" | "reject" | "abort";

// 开发期走 vite 代理 /api → 后端;生产可注入 VITE_API_BASE。
const BASE = (import.meta as { env?: Record<string, string> }).env?.VITE_API_BASE || "/api";

export async function postCommand(text: string, sessionId: string): Promise<CommandResp> {
  const r = await fetch(`${BASE}/command`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, session_id: sessionId, channel: "web" }),
  });
  if (!r.ok) throw new Error(`command failed: ${r.status}`);
  return r.json();
}

export async function postDecision(
  taskId: string,
  verdict: Verdict,
  reason = "",
): Promise<void> {
  const r = await fetch(`${BASE}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId, verdict, reason }),
  });
  if (!r.ok) throw new Error(`decision failed: ${r.status}`);
}

export async function getCost(taskId: string): Promise<CostResp> {
  const r = await fetch(`${BASE}/cost?task_id=${encodeURIComponent(taskId)}`);
  if (!r.ok) throw new Error(`cost failed: ${r.status}`);
  return r.json();
}

export async function getTask(taskId: string): Promise<TaskSnapshot> {
  const r = await fetch(`${BASE}/task/${encodeURIComponent(taskId)}`);
  if (!r.ok) throw new Error(`task failed: ${r.status}`);
  return r.json();
}

export function eventsUrl(taskId: string): string {
  return `${BASE}/events?task_id=${encodeURIComponent(taskId)}`;
}
