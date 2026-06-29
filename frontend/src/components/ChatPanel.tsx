import { useState } from "react";

interface Props {
  onSubmit: (text: string) => void;
  disabled: boolean;
}

const SAMPLES = [
  "搭建一个最小电商下单接口:商品列表 + 下单 + 订单查询。",
  "做一个支持标签筛选的待办清单 Web 应用。",
];

export function ChatPanel({ onSubmit, disabled }: Props) {
  const [text, setText] = useState("");

  const submit = () => {
    const t = text.trim();
    if (!t || disabled) return;
    onSubmit(t);
    setText("");
  };

  return (
    <div className="panel chat">
      <div className="panel-title">下达指令</div>
      <textarea
        value={text}
        placeholder="把你的需求告诉一人公司…（Cmd/Ctrl + Enter 发送）"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit();
        }}
        rows={4}
      />
      <div className="chat-actions">
        <div className="samples">
          {SAMPLES.map((s) => (
            <button key={s} className="chip" onClick={() => setText(s)}>
              {s.slice(0, 14)}…
            </button>
          ))}
        </div>
        <button className="primary" disabled={disabled || !text.trim()} onClick={submit}>
          {disabled ? "编排中…" : "发送"}
        </button>
      </div>
    </div>
  );
}
