import { CostResp } from "../api/client";

export function CostPanel({ cost }: { cost: CostResp | null }) {
  return (
    <div className="panel cost">
      <div className="panel-title">成本仪表</div>
      {!cost && <div className="empty">任务结束后显示成本聚合。</div>}
      {cost && (
        <>
          <div className="cost-total">
            <div className="cost-metric">
              <span className="num">{cost.total_tokens.toLocaleString()}</span>
              <span className="lbl">总 tokens</span>
            </div>
            <div className="cost-metric">
              <span className="num">{cost.total_latency_ms.toLocaleString()}</span>
              <span className="lbl">总延迟 ms</span>
            </div>
          </div>
          <table className="cost-table">
            <thead>
              <tr>
                <th>角色</th>
                <th>tokens</th>
                <th>调用</th>
                <th>延迟 ms</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(cost.by_role).map(([role, c]) => (
                <tr key={role}>
                  <td>{role}</td>
                  <td>{c.tokens.toLocaleString()}</td>
                  <td>{c.calls}</td>
                  <td>{c.latency_ms.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
