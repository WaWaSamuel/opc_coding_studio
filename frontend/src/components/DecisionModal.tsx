import { OpcEvent, Verdict } from "../api/client";

interface Props {
  event: OpcEvent;
  onDecide: (verdict: Verdict) => void;
}

// need_decision 人在环弹窗:等价飞书卡片按钮回调,放行/返工/终止。
export function DecisionModal({ event, onDecide }: Props) {
  const note = String(event.payload?.note ?? "编排器请求宿主拍板。");
  return (
    <div className="modal-mask">
      <div className="modal">
        <div className="modal-title">需要你拍板</div>
        <p className="modal-note">{note}</p>
        <div className="modal-actions">
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
      </div>
    </div>
  );
}
