import { OpcEvent } from "../api/client";

interface TodoItem {
  id?: string;
  desc?: string;
  owner_role?: string;
  status?: string;
}

// 从事件流里取最近一次 dev_plan / 任务快照里的 TODO。这里用事件推导,
// 真源仍是后端 state.todo_plan(可经 GET /task 拉取)。
export function TodoView({ events, todoPlan }: { events: OpcEvent[]; todoPlan: TodoItem[] }) {
  const planEvent = [...events].reverse().find((e) => e.event === "dev_plan");
  const count = todoPlan.length || Number(planEvent?.payload?.todo_items ?? 0);

  return (
    <div className="panel todo">
      <div className="panel-title">TODO 进度 ({count})</div>
      {todoPlan.length === 0 && (
        <div className="empty">
          {count > 0 ? `部长已拆解 ${count} 条 TODO（详情见任务快照）。` : "尚未拆解 TODO。"}
        </div>
      )}
      <ul className="todo-list">
        {todoPlan.map((t, i) => (
          <li key={t.id || i} className={`todo-${t.status || "todo"}`}>
            <span className="todo-id">{t.id || `T${i + 1}`}</span>
            <span className="todo-desc">{t.desc}</span>
            {t.owner_role && <span className="todo-owner">{t.owner_role}</span>}
            <span className="todo-status">{t.status || "todo"}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
