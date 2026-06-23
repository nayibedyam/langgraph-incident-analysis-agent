import { useState } from 'react';
import { Loader2, ScanSearch, Sparkles } from 'lucide-react';
import { api } from '@/api';
import { Markdown } from './Markdown';

/** Paste-a-log first-line triage agent (LLM). */
export function TriageView({ onOpenDefect }: { onOpenDefect?: (id: string) => void }) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [matched, setMatched] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.triageLog(text);
      if (r.error) setError(r.error);
      else {
        setResult(r.markdown);
        setMatched(r.matched_ids);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="triage">
      <div className="triage__head">
        <h2 className="overview__title">Log Triage</h2>
        <p className="overview__sub">
          Paste raw logs, a syslog burst, or an error trace. The agent extracts signatures,
          infers the probable root cause, and matches it against analyzed defects.
        </p>
      </div>

      <div className="triage__grid">
        <div className="triage__input-col">
          <textarea
            className="triage__textarea"
            placeholder="Paste logs / syslog / traceback here…"
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck={false}
          />
          <button className="analyze__btn triage__btn" onClick={() => void run()} disabled={busy || !text.trim()}>
            {busy ? <Loader2 size={15} className="spin" /> : <ScanSearch size={15} />}
            {busy ? 'Triaging…' : 'Triage'}
          </button>
        </div>

        <div className="triage__result-col">
          {error && <div className="agent-card__error">{error}</div>}
          {!result && !error && !busy && (
            <div className="triage__placeholder">
              <Sparkles size={26} />
              <p>Triage output will appear here.</p>
            </div>
          )}
          {busy && <div className="agent-card__muted">Analyzing signatures and matching defects…</div>}
          {result && (
            <div className="agent-card">
              {matched.length > 0 && (
                <div className="triage__matches">
                  {matched.map((id) => (
                    <button key={id} className="taglet" onClick={() => onOpenDefect?.(id)}>
                      {id}
                    </button>
                  ))}
                </div>
              )}
              <Markdown>{result}</Markdown>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
