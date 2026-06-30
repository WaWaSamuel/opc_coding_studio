import { OpcEvent, Verdict } from "../api/client";

interface Props {
  event: OpcEvent;
  onDecide: (verdict: Verdict) => void;
}

// F-A.7 通道①:对话式人在环决策气泡(替代全屏 DecisionModal)。
// 既给出按钮,也提示可直接在对话框打字回复("通过/打回/终止"经 classify_decision 解析)。
export function InlineDecision({ event, onDecide }: Props) {
  const note = String(event.payload?.note ?? "编排器请求宿主拍板。");
  return (
    <div className="inline-decision">
      <div className="id-title">需要你拍板</div>
      <div className="id-note">{note}</div>
      <div className="id-actions">
        <button className="primary" onClick={() => onDecide("pass")}>
          放行收口
        </button>
        <button className="warn" onClick={() => onDecide("reject")}>
          打回返工
        </button>
        <button className="danger" onClick={() => onDecide("abort")}>
          终止任务
        </button>
      </div>
      <div className="id-hint">
        也可直接在下方对话框回复（如「通过」「打回，再改下颜色」「终止」）。
      </div>
    </div>
  );
}
