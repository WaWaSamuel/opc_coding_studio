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

// F-A.9/A.10:对话消息 + 多模态附件
export interface ChatAttachment {
  type: "image";
  url: string;
  name?: string;
  content_type?: string;
}
export interface ChatMessage {
  role: "host" | "assistant";
  text: string;
  ts: number;
  attachments?: ChatAttachment[];
  // F-A.11 终态回执气泡着色(done 绿 / error 红);仅前端渲染,不入后端契约。
  tone?: "done" | "error";
}

// 开发期走 vite 代理 /api → 后端;生产可注入 VITE_API_BASE。
const BASE = (import.meta as { env?: Record<string, string> }).env?.VITE_API_BASE || "/api";

export async function postCommand(
  text: string,
  sessionId: string,
  intent?: "runtime" | "edit",
  opts?: { messages?: ChatMessage[]; attachments?: ChatAttachment[] },
): Promise<CommandResp> {
  const r = await fetch(`${BASE}/command`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text,
      session_id: sessionId,
      channel: "web",
      intent,
      messages: opts?.messages ?? [],
      attachments: opts?.attachments ?? [],
    }),
  });
  if (!r.ok) throw new Error(`command failed: ${r.status}`);
  return r.json();
}

// F-A.10 多模态图片上传 → 返回可回访 url(供 attachments 透传后端多模态模型)。
export interface UploadResp {
  url: string;
  name: string;
  content_type: string;
  size: number;
}
export async function postUpload(file: File): Promise<UploadResp> {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch(`${BASE}/upload`, { method: "POST", body: fd });
  if (!r.ok) throw new Error(`upload failed: ${r.status}`);
  return r.json();
}

// 把后端返回的相对 url(/uploads/xxx)拼到 API base 上供 <img> 直接访问。
export function assetUrl(url: string): string {
  if (!url || url.startsWith("http") || url.startsWith("data:")) return url;
  return `${BASE}${url}`;
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

// F-A.7 通道①:把宿主的对话式自然语言回复交后端 classify_decision 归一为 verdict。
// 解析成功 → 返回归一后的 verdict;无法判定(后端 400)→ 返回 null,交按钮兜底。
export async function postDecisionText(
  taskId: string,
  text: string,
): Promise<Verdict | null> {
  const r = await fetch(`${BASE}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId, text }),
  });
  if (r.status === 400) return null;
  if (!r.ok) throw new Error(`decision failed: ${r.status}`);
  const data = (await r.json()) as { verdict?: Verdict };
  return data.verdict ?? null;
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

// ── Edit 系统(M5 / F-A.8 可视化 + F-E.4 受控 PR + M6 多工作流/角色详情)──
export interface EditNode {
  id: string;
  label: string;
  kind: string;
  changed?: boolean;
  role_id?: string;
}
export interface EditEdge {
  from: string;
  to: string;
  label?: string;
}
export interface WorkflowRef {
  id: string;
  label: string;
}
export interface EditGraphResp {
  ref: string;
  workflow?: string;
  workflows?: WorkflowRef[];
  nodes: EditNode[];
  edges: EditEdge[];
  git?: { enabled: boolean; can_push: boolean; main_branch: string };
}

// F-A.8 角色详情(RoleInspector)
export interface RoleDetail {
  role_id: string;
  model_tier: string;
  responsibility: string;
  output_schema_keys: string[];
  tools: string[];
  skills: string[];
}

export interface EditPrResp {
  pr_url: string;
  branch: string;
  title: string;
  pushed: boolean;
  dry_run: boolean;
}

export async function getEditGraph(
  ref = "main",
  workflow = "edit",
): Promise<EditGraphResp> {
  const r = await fetch(
    `${BASE}/edit/graph?ref=${encodeURIComponent(ref)}&workflow=${encodeURIComponent(workflow)}`,
  );
  if (!r.ok) throw new Error(`edit graph failed: ${r.status}`);
  return r.json();
}

export async function getRole(roleId: string): Promise<RoleDetail> {
  const r = await fetch(`${BASE}/role/${encodeURIComponent(roleId)}`);
  if (!r.ok) throw new Error(`role failed: ${r.status}`);
  return r.json();
}

export interface RestartResp {
  ok: boolean;
  scope: string;
  dry_run: boolean;
  health: Record<string, unknown>;
  note: string;
}
export async function postEditRestart(scope = "both"): Promise<RestartResp> {
  const r = await fetch(`${BASE}/edit/restart`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope }),
  });
  if (!r.ok) throw new Error(`restart failed: ${r.status}`);
  return r.json();
}

export async function postEditPr(
  branch: string,
  summary = "",
  badcaseRef = "",
): Promise<EditPrResp> {
  const r = await fetch(`${BASE}/edit/pr`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ branch, summary, badcase_ref: badcaseRef }),
  });
  if (!r.ok) throw new Error(`edit pr failed: ${r.status}`);
  return r.json();
}
