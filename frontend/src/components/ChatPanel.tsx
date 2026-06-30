import { useRef, useState } from "react";
import {
  assetUrl,
  ChatAttachment,
  ChatMessage,
  OpcEvent,
  postUpload,
  Verdict,
} from "../api/client";
import { InlineDecision } from "./InlineDecision";

interface Props {
  messages: ChatMessage[];
  onSubmit: (text: string, attachments: ChatAttachment[]) => void;
  disabled: boolean;
  pendingDecision: OpcEvent | null;
  onDecide: (verdict: Verdict) => void;
}

const SAMPLES = [
  "搭建一个最小电商下单接口:商品列表 + 下单 + 订单查询。",
  "修改 web style 颜色为粉色。",
];

export function ChatPanel({
  messages,
  onSubmit,
  disabled,
  pendingDecision,
  onDecide,
}: Props) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const threadRef = useRef<HTMLDivElement | null>(null);

  const addFiles = async (files: FileList | File[]) => {
    const imgs = Array.from(files).filter((f) => f.type.startsWith("image/"));
    if (imgs.length === 0) return;
    setUploading(true);
    try {
      for (const f of imgs) {
        const up = await postUpload(f);
        setAttachments((prev) => [
          ...prev,
          { type: "image", url: up.url, name: up.name, content_type: up.content_type },
        ]);
      }
    } catch {
      /* 上传失败静默,允许重试 */
    } finally {
      setUploading(false);
    }
  };

  const submit = () => {
    const t = text.trim();
    if ((!t && attachments.length === 0) || disabled) return;
    onSubmit(t, attachments);
    setText("");
    setAttachments([]);
    // 滚到底部由 messages 更新后浏览器自然处理;主动兜一次。
    requestAnimationFrame(() => {
      if (threadRef.current) threadRef.current.scrollTop = threadRef.current.scrollHeight;
    });
  };

  return (
    <div className="panel chat">
      <div className="panel-title">对话</div>

      <div className="chat-thread" ref={threadRef}>
        {messages.length === 0 && (
          <div className="empty">把你的需求告诉一人公司，支持多轮对话与图片。</div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div className={`msg-bubble ${m.tone ?? ""}`}>
              {m.text}
              {m.attachments && m.attachments.length > 0 && (
                <div className="msg-imgs">
                  {m.attachments.map((a, j) => (
                    <img key={j} src={assetUrl(a.url)} alt={a.name || "image"} />
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      {pendingDecision && (
        <InlineDecision event={pendingDecision} onDecide={onDecide} />
      )}

      {attachments.length > 0 && (
        <div className="attach-preview">
          {attachments.map((a, i) => (
            <div className="attach-chip" key={i}>
              <img src={assetUrl(a.url)} alt={a.name || "image"} />
              <button
                className="x"
                onClick={() =>
                  setAttachments((prev) => prev.filter((_, k) => k !== i))
                }
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      <textarea
        value={text}
        placeholder="把你的需求/回复告诉一人公司…（Cmd/Ctrl + Enter 发送，可粘贴图片）"
        onChange={(e) => setText(e.target.value)}
        onPaste={(e) => {
          const items = Array.from(e.clipboardData?.files || []);
          if (items.length) {
            e.preventDefault();
            addFiles(items);
          }
        }}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit();
        }}
        rows={3}
      />

      <div className="chat-actions">
        <div className="samples">
          {SAMPLES.map((s) => (
            <button key={s} className="chip" onClick={() => setText(s)}>
              {s.slice(0, 14)}…
            </button>
          ))}
        </div>
        <div className="chat-tools">
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) addFiles(e.target.files);
              e.target.value = "";
            }}
          />
          <button
            className="icon-btn"
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
          >
            {uploading ? "上传中…" : "图片"}
          </button>
          <button
            className="primary"
            disabled={disabled || (!text.trim() && attachments.length === 0)}
            onClick={submit}
          >
            {disabled ? "编排中…" : "发送"}
          </button>
        </div>
      </div>
    </div>
  );
}
