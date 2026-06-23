import { useEffect, useState } from 'react';
import { GitBranch, Loader2, MessageSquare, Send, Sparkles, Wrench } from 'lucide-react';
import { api } from '@/api';
import type { SimilarDefect } from '@/types';
import { Markdown } from './Markdown';

/** Priority badge (P1..P4) used across the app. */
export function PriorityBadge({ priority, title }: { priority: string; title?: string }) {
  const tone = priority === 'P1' ? 'p1' : priority === 'P2' ? 'p2' : priority === 'P3' ? 'p3' : 'p4';
  return (
    <span className={`prio prio--${tone}`} title={title}>
      {priority}
    </span>
  );
}

/** Related past defects (deterministic similarity). */
export function RelatedPanel({
  cdetsId,
  onOpen,
}: {
  cdetsId: string;
  onOpen: (id: string) => void;
}) {
  const [items, setItems] = useState<SimilarDefect[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setError(null);
    api
      .similar(cdetsId)
      .then((r) => !cancelled && setItems(r.neighbors))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [cdetsId]);

  return (
    <section className="agent-card">
      <h3 className="agent-card__title">
        <GitBranch size={15} /> Related Past Defects
      </h3>
      {error && <div className="agent-card__error">{error}</div>}
      {!items && !error && <div className="agent-card__muted">Finding related defects…</div>}
      {items && items.length === 0 && (
        <div className="agent-card__muted">No closely related defects found.</div>
      )}
      {items && items.length > 0 && (
        <ul className="related-list">
          {items.map((n) => (
            <li key={n.cdets_id}>
              <button className="related-item" onClick={() => onOpen(n.cdets_id)}>
                <span className="related-item__top">
                  <span className="related-item__id">{n.cdets_id}</span>
                  <span className="related-item__sim">{n.similarity}%</span>
                </span>
                <span className="related-item__headline">{n.headline ?? '—'}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** On-demand LLM card (remediation or enrichment). */
export function AgentMarkdownCard({
  cdetsId,
  kind,
}: {
  cdetsId: string;
  kind: 'remediation' | 'enrichment';
}) {
  const [md, setMd] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const meta =
    kind === 'remediation'
      ? { title: 'AI Remediation', icon: <Wrench size={15} />, cta: 'Generate remediation' }
      : { title: 'Ticket Enrichment Note', icon: <Sparkles size={15} />, cta: 'Draft enrichment note' };

  const run = () => {
    setLoading(true);
    setError(null);
    const call = kind === 'remediation' ? api.remediation(cdetsId) : api.enrichment(cdetsId);
    call
      .then((r) => {
        if (r.error) setError(r.error);
        else setMd(r.markdown);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  };

  // Reset when switching defects.
  useEffect(() => {
    setMd(null);
    setError(null);
  }, [cdetsId]);

  return (
    <section className="agent-card">
      <h3 className="agent-card__title">
        {meta.icon} {meta.title}
      </h3>
      {!md && (
        <button className="agent-btn" onClick={run} disabled={loading}>
          {loading ? <Loader2 size={14} className="spin" /> : meta.icon}
          {loading ? 'Generating…' : meta.cta}
        </button>
      )}
      {error && <div className="agent-card__error">{error}</div>}
      {md && (
        <div className="agent-card__body">
          {kind === 'enrichment' ? (
            <pre className="enrich-note">{md}</pre>
          ) : (
            <Markdown>{md}</Markdown>
          )}
          <button className="agent-link" onClick={run} disabled={loading}>
            {loading ? 'Regenerating…' : 'Regenerate'}
          </button>
        </div>
      )}
    </section>
  );
}

interface ChatTurn {
  role: 'user' | 'assistant';
  text: string;
}

/** Conversational Q&A grounded in one defect. */
export function ChatPanel({ cdetsId }: { cdetsId: string }) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setTurns([]);
    setInput('');
  }, [cdetsId]);

  const send = async () => {
    const q = input.trim();
    if (!q || busy) return;
    setInput('');
    setTurns((t) => [...t, { role: 'user', text: q }]);
    setBusy(true);
    try {
      const r = await api.chat(cdetsId, q);
      setTurns((t) => [...t, { role: 'assistant', text: r.error ? `⚠️ ${r.error}` : r.answer }]);
    } catch (e) {
      setTurns((t) => [
        ...t,
        { role: 'assistant', text: `⚠️ ${e instanceof Error ? e.message : String(e)}` },
      ]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="agent-card">
      <h3 className="agent-card__title">
        <MessageSquare size={15} /> Ask about this defect
      </h3>
      <div className="chat-thread">
        {turns.length === 0 && (
          <div className="agent-card__muted">
            Ask anything — e.g. “What triggers this?”, “Is there a workaround?”, “Which AP owns it?”
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className={`chat-turn chat-turn--${t.role}`}>
            <div className="chat-turn__role">{t.role === 'user' ? 'You' : 'Agent'}</div>
            <div className="chat-turn__text">
              {t.role === 'assistant' ? <Markdown>{t.text}</Markdown> : t.text}
            </div>
          </div>
        ))}
        {busy && <div className="agent-card__muted">Agent is thinking…</div>}
      </div>
      <div className="chat-input">
        <input
          value={input}
          placeholder="Ask a question…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && void send()}
          disabled={busy}
        />
        <button onClick={() => void send()} disabled={busy || !input.trim()}>
          {busy ? <Loader2 size={14} className="spin" /> : <Send size={14} />}
        </button>
      </div>
    </section>
  );
}
