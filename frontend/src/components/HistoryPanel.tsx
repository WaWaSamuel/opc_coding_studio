import { useEffect, useState } from "react";
import { getTasks, TaskListItem } from "../api/client";

// F-A.12 历史会话面板:最近更新优先,按当前视图(runtime|edit)过滤;
// 点选某条 → 回放该任务(父组件 setTaskId 拉历史事件 + 快照)。
// 列表先吃 localStorage 缓存秒开,再后台拉最新覆盖(可查 + 可缓存 + 可加载)。
const CACHE_KEY = "opc.history";

function loadCache(system: "runtime" | "edit"): TaskListItem[] {
  try {
    const raw = localStorage.getItem(`${CACHE_KEY}.${system}`);
    return raw ? (JSON.parse(raw) as TaskListItem[]) : [];
  } catch {
    return [];
  }
}

function saveCache(system: "runtime" | "edit", items: TaskListItem[]): void {
  try {
    localStorage.setItem(`${CACHE_KEY}.${system}`, JSON.stringify(items));
  } catch {
    /* 配额满/隐私模式:忽略,缓存只是加速,非真源 */
  }
}

export function HistoryPanel({
  system,
  activeTaskId,
  onPick,
  refreshKey,
}: {
  system: "runtime" | "edit";
  activeTaskId: string | null;
  onPick: (taskId: string) => void;
  refreshKey?: number;
}) {
  const [items, setItems] = useState<TaskListItem[]>(() => loadCache(system));

  useEffect(() => {
    // 切视图:先显缓存,再拉最新
    setItems(loadCache(system));
    let alive = true;
    getTasks(system)
      .then((list) => {
        if (!alive) return;
        setItems(list);
        saveCache(system, list);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [system, refreshKey]);

  return (
    <div className="panel history">
      <div className="panel-title">历史会话 ({items.length})</div>
      {items.length === 0 && <div className="empty">暂无历史会话。</div>}
      <ul className="history-list">
        {items.map((it) => (
          <li
            key={it.task_id}
            className={`history-item ${it.task_id === activeTaskId ? "active" : ""}`}
            onClick={() => onPick(it.task_id)}
            title={it.title}
          >
            <span className={`history-status ${it.status}`}>{it.status}</span>
            <span className="history-title">{it.title || it.task_id}</span>
            <span className="history-id">{it.task_id}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
