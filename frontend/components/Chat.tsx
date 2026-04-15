import { useEffect, useRef } from "react";
import type { Message } from "../pages/index";

interface ChatProps {
  messages:   Message[];
  isThinking: boolean;
}

export default function Chat({ messages, isThinking }: ChatProps) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isThinking]);

  const formatTime = (d: Date) => {
    try {
      return new Date(d).toLocaleTimeString("en-US", {
        hour: "2-digit", minute: "2-digit", hour12: true
      });
    } catch { return ""; }
  };

  return (
    <div className="chat-container">
      {messages.length === 0 && !isThinking && (
        <div className="chat-empty">
          Olá Simão! O que precisas hoje?
        </div>
      )}

      {messages.map(msg => (
        <div key={msg.id} className={`message message-${msg.role}`}>
          <div className={`message-bubble ${msg.role === "user" ? "user-bubble" : "assistant-bubble"}`}>
            <MessageContent content={msg.content} />
          </div>
          <div className="message-time">{formatTime(msg.timestamp)}</div>
        </div>
      ))}

      {isThinking && (
        <div className="message message-assistant">
          <div className="message-bubble assistant-bubble thinking-dots">
            <span /><span /><span />
          </div>
        </div>
      )}

      <div ref={endRef} />
    </div>
  );
}

function MessageContent({ content }: { content: string }) {
  const lines = content.split("\n");
  return (
    <>
      {lines.map((line, i) => {
        if (line.startsWith("### ")) return <h3 key={i}>{line.slice(4)}</h3>;
        if (line.startsWith("## "))  return <h2 key={i}>{line.slice(3)}</h2>;
        if (line.startsWith("# "))   return <h1 key={i}>{line.slice(2)}</h1>;
        if (line.startsWith("- ") || line.startsWith("* "))
          return <li key={i}>{line.slice(2)}</li>;
        if (line === "") return <br key={i} />;
        const parts = line.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
        return (
          <span key={i}>
            {parts.map((p, j) => {
              if (p.startsWith("`") && p.endsWith("`"))
                return <code key={j}>{p.slice(1, -1)}</code>;
              if (p.startsWith("**") && p.endsWith("**"))
                return <strong key={j}>{p.slice(2, -2)}</strong>;
              return p;
            })}
            {i < lines.length - 1 && <br />}
          </span>
        );
      })}
    </>
  );
}
