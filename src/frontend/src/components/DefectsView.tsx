import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  FileCode2,
  FileText,
  LayoutDashboard,
  Loader2,
  RefreshCw,
  Search,
  ShieldAlert,
  Sparkles,
  Trash2,
  Zap,
} from 'lucide-react';
import { api } from '@/api';
import type { ArtifactDetail, ArtifactFile, ArtifactSummary, ModelChoice } from '@/types';
import { FileViewer } from './FileViewer';
import { PriorityBadge } from './AgentPanels';
import { AnalyzeProgress } from './AnalyzeProgress';
import { RunSummaryPanel } from './RunSummaryPanel';

function Chip({ label, value }: { label: string; value?: string }) {
  if (!value) return null;
  return (
    <span className="chip">
      <span className="chip__label">{label}</span>
      <span className="chip__value">{value}</span>
    </span>
  );
}

function fileIcon(kind: ArtifactFile['kind']) {
  return kind === 'json' ? <FileCode2 size={14} /> : <FileText size={14} />;
}

function fmtTok(n: number): string {
  if (!n) return '0';
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function TokenUsagePanel({ detail }: { detail: ArtifactDetail }) {
  const totals = detail.token_totals;
  const traces = detail.stage_traces;
  if (!totals || !traces) return null;
  const rows = Object.entries(traces)
    .map(([node, t]) => ({
      node,
      input: Number(t?.input_tokens ?? 0) || 0,
      output: Number(t?.output_tokens ?? 0) || 0,
    }))
    .filter((r) => r.input > 0 || r.output > 0)
    .sort((a, b) => b.input + b.output - (a.input + a.output));
  if (!rows.length) return null;

  return (
    <section className="token-panel">
      <header className="token-panel__head">
        <h4>Token usage</h4>
        <span className="progress__tokens">
          <span className="tok tok--in">↑ {fmtTok(totals.input)}</span>
          <span className="tok tok--out">↓ {fmtTok(totals.output)}</span>
          <span className="tok tok--sum">Σ {fmtTok(totals.total)}</span>
        </span>
      </header>
      <table className="token-table">
        <thead>
          <tr>
            <th>Stage</th>
            <th>Input</th>
            <th>Output</th>
            <th>Total</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.node}>
              <td className="token-table__node">{r.node}</td>
              <td><span className="tok tok--in">↑ {fmtTok(r.input)}</span></td>
              <td><span className="tok tok--out">↓ {fmtTok(r.output)}</span></td>
              <td><span className="tok tok--sum">{fmtTok(r.input + r.output)}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function DefectsView({ initialId }: { initialId?: string | null }) {
  const [summaries, setSummaries] = useState<ArtifactSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(initialId ?? null);
  const [detail, setDetail] = useState<ArtifactDetail | null>(null);
  const [activeFile, setActiveFile] = useState<string | null>(null);
  const [tab, setTab] = useState<'summary' | 'artifacts'>('summary');
  const [detailTick, setDetailTick] = useState(0);

  const [analyzeId, setAnalyzeId] = useState('');
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeMsg, setAnalyzeMsg] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null);
  // Live LangGraph job in flight (drives the AnalyzeProgress panel).
  const [progressFor, setProgressFor] = useState<string | null>(null);
  // User-chosen model for the cdets_tz_analyzer stage; sticky via localStorage.
  const [model, setModel] = useState<ModelChoice>(() => {
    const saved = typeof window !== 'undefined' ? window.localStorage.getItem('fl.model') : null;
    return saved === 'sonnet' || saved === 'opus' ? (saved as ModelChoice) : 'opus';
  });
  useEffect(() => {
    try {
      window.localStorage.setItem('fl.model', model);
    } catch {
      /* ignore */
    }
  }, [model]);

  const load = useCallback((selectId?: string) => {
    setLoading(true);
    setError(null);
    return api
      .listArtifacts()
      .then((rows) => {
        setSummaries(rows);
        // Do not auto-select a defect: keep the centered home screen until the
        // user explicitly picks one or analyzes a new CDETS ID.
        setSelectedId((cur) => selectId ?? cur ?? null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    api
      .getArtifact(selectedId)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        // Keep the current file selected if it still exists; otherwise default
        // to the scorecard (or first file). This avoids the view jumping when
        // background generation adds new files.
        setActiveFile((cur) => {
          if (cur && d.files.some((f) => f.name === cur)) return cur;
          return (
            d.files.find((f) => f.name.includes('-Scorecard'))?.name ?? d.files[0]?.name ?? null
          );
        });
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [selectedId, detailTick]);

  const handleAnalyze = useCallback(async () => {
    // CDETS IDs are case-sensitive: CSC upper, two product letters lower.
    const trimmed = analyzeId.trim();
    const id = /^csc/i.test(trimmed)
      ? 'CSC' + trimmed.slice(3, 5).toLowerCase() + trimmed.slice(5)
      : trimmed;
    if (!/^CSC[a-z]{2}[0-9]{5}$/.test(id)) {
      setAnalyzeMsg({ tone: 'error', text: 'Enter a valid CDETS ID, e.g. CSCwu28811.' });
      return;
    }
    setAnalyzing(true);
    setAnalyzeMsg(null);
    setProgressFor(id);
    setAnalyzeId('');
    setAnalyzing(false);
  }, [analyzeId]);

  // Called by <AnalyzeProgress/> when the pipeline finishes (success or failure).
  const handleProgressDone = useCallback(
    async (id: string, ok: boolean) => {
      setProgressFor(null);
      if (ok) {
        setAnalyzeMsg({ tone: 'ok', text: `${id}: analysis complete.` });
        await load(id);
        setDetailTick((t) => t + 1);
        setTab('summary');
      }
    },
    [load],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return summaries;
    return summaries.filter(
      (s) =>
        s.cdets_id.toLowerCase().includes(q) ||
        (s.headline ?? '').toLowerCase().includes(q) ||
        (s.scores.primary_ap ?? '').toLowerCase().includes(q),
    );
  }, [summaries, query]);

  const handleDelete = useCallback(
    async (id: string, e: React.MouseEvent) => {
      e.stopPropagation();
      if (!window.confirm(`Delete all analysis artifacts for ${id}? This cannot be undone.`)) {
        return;
      }
      try {
        await api.deleteArtifact(id);
        setSummaries((rows) => rows.filter((r) => r.cdets_id !== id));
        setSelectedId((cur) => (cur === id ? null : cur));
        setDetail((cur) => (cur && cur.cdets_id === id ? null : cur));
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [],
  );

  const selectedFile = useMemo(
    () => detail?.files.find((f) => f.name === activeFile) ?? null,
    [detail, activeFile],
  );

  // The home (ChatGPT-style) screen shows whenever no defect is open. The
  // centered analyze box lives there; the sidebar offers a "New analysis"
  // shortcut back to it.
  const showHome = !detail;
  const startNew = useCallback(() => {
    setSelectedId(null);
    setDetail(null);
    setAnalyzeMsg(null);
    setQuery('');
  }, []);

  const recent = useMemo(() => summaries.slice(0, 6), [summaries]);

  return (
    <div className={`defects ${showHome ? 'defects--home' : ''}`}>
      <aside className="sidebar">
        <button className="new-analysis" onClick={startNew}>
          <Sparkles size={15} /> New analysis
        </button>

        <div className="sidebar__filter">
          <Search size={13} />
          <input
            placeholder="Filter analyzed defects…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button className="icon-btn" onClick={() => void load()} title="Refresh">
            <RefreshCw size={13} />
          </button>
        </div>

        {loading && summaries.length === 0 ? (
          <div className="sidebar__status">Loading…</div>
        ) : filtered.length === 0 ? (
          <div className="sidebar__status">No matching defects.</div>
        ) : (
          <ul className="defect-list">
            {filtered.map((s) => (
              <li key={s.cdets_id} className="defect-row">
                <button
                  className={`defect-item ${s.cdets_id === selectedId ? 'defect-item--active' : ''}`}
                  onClick={() => setSelectedId(s.cdets_id)}
                >
                  <span className="defect-item__id">{s.cdets_id}</span>
                  <span className="defect-item__headline">{s.headline ?? '—'}</span>
                  <span className="defect-item__meta">
                    <PriorityBadge priority={s.priority.priority} title={s.priority.rationale} />
                    {s.scores.cdets_defect_score && (
                      <span className="defect-item__score">{s.scores.cdets_defect_score}</span>
                    )}
                    {s.scores.primary_ap && <span className="taglet">{s.scores.primary_ap}</span>}
                    <span className="defect-item__count">{s.file_count} files</span>
                  </span>
                </button>
                <button
                  className="defect-del"
                  title={`Delete ${s.cdets_id}`}
                  aria-label={`Delete ${s.cdets_id}`}
                  onClick={(e) => void handleDelete(s.cdets_id, e)}
                >
                  <Trash2 size={14} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <section className="detail">
        {error && <div className="view-status view-status--error">{error}</div>}
        {showHome && !error && (
          <div className="home">
            <div className="home__inner">
              <span className="home__badge">
                <Sparkles size={14} /> IOS-XR Feedback Loop
              </span>
              <h1 className="home__title">What defect can I analyze for you?</h1>
              <p className="home__sub">
                Enter a CDETS ID to generate a quality scorecard, CaFy RCA, and a lab-ready test
                case.
              </p>

              <div className="home__box">
                <Search size={18} className="home__box-icon" />
                <input
                  className="home__input"
                  placeholder="Analyze CDETS ID, e.g. CSCwu28811"
                  value={analyzeId}
                  onChange={(e) => setAnalyzeId(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && void handleAnalyze()}
                  disabled={analyzing}
                  spellCheck={false}
                  autoFocus
                />
                <div className="model-seg" role="radiogroup" aria-label="Model for CDETS analyzer">
                  <button
                    type="button"
                    role="radio"
                    aria-checked={model === 'sonnet'}
                    className={`model-seg__btn ${model === 'sonnet' ? 'model-seg__btn--on' : ''}`}
                    onClick={() => setModel('sonnet')}
                    title="Sonnet — faster, lower cost"
                  >
                    <Zap size={12} /> Sonnet
                  </button>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={model === 'opus'}
                    className={`model-seg__btn ${model === 'opus' ? 'model-seg__btn--on' : ''}`}
                    onClick={() => setModel('opus')}
                    title="Opus — deeper analysis"
                  >
                    <Sparkles size={12} /> Opus
                  </button>
                </div>
                <button
                  className="home__btn"
                  onClick={() => void handleAnalyze()}
                  disabled={analyzing || !analyzeId.trim()}
                >
                  {analyzing ? <Loader2 size={15} className="spin" /> : 'Analyze'}
                </button>
              </div>

              {analyzeMsg && (
                <div className={`analyze__msg analyze__msg--${analyzeMsg.tone}`}>
                  {analyzeMsg.text}
                </div>
              )}

              {progressFor && (
                <AnalyzeProgress
                  cdetsId={progressFor}
                  model={model}
                  onDone={() => void handleProgressDone(progressFor, true)}
                  onError={(err) => {
                    setAnalyzeMsg({ tone: 'error', text: `${progressFor}: ${err}` });
                    void handleProgressDone(progressFor, false);
                  }}
                />
              )}

              {recent.length > 0 && (
                <div className="home__recent">
                  <span className="home__recent-label">Recent</span>
                  <div className="home__chips">
                    {recent.map((s) => (
                      <button
                        key={s.cdets_id}
                        className="home__chip"
                        onClick={() => setSelectedId(s.cdets_id)}
                        title={s.headline ?? undefined}
                      >
                        <PriorityBadge priority={s.priority.priority} />
                        {s.cdets_id}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
        {detail && (
          <>
            <header className="detail__head">
              <div className="detail__title">
                <ShieldAlert size={18} />
                <div>
                  <div className="detail__id">
                    {detail.cdets_id}
                    <PriorityBadge
                      priority={detail.priority.priority}
                      title={detail.priority.rationale}
                    />
                    {detail.regression && <span className="taglet taglet--warn">regression</span>}
                  </div>
                  <div className="detail__headline">{detail.headline ?? ''}</div>
                </div>
              </div>
              <div className="detail__chips">
                <Chip label="CDETS" value={detail.scores.cdets_defect_score} />
                <Chip label="AI" value={detail.scores.ai_confidence} />
                <Chip label="Automation" value={detail.scores.automation_readiness} />
                <Chip label="Coverage" value={detail.scores.test_coverage_confidence} />
                <Chip label="Gap" value={detail.scores.coverage_gap} />
                <Chip label="AP" value={detail.scores.primary_ap} />
                <Chip label="Sub-AP" value={detail.scores.sub_ap} />
                <Chip label="Severity" value={detail.severity ?? undefined} />
              </div>
              <div className="detail__viewtabs">
                <button
                  className={`vtab ${tab === 'summary' ? 'vtab--active' : ''}`}
                  onClick={() => setTab('summary')}
                >
                  <LayoutDashboard size={14} /> Summary
                </button>
                <button
                  className={`vtab ${tab === 'artifacts' ? 'vtab--active' : ''}`}
                  onClick={() => setTab('artifacts')}
                >
                  <FileText size={14} /> Artifacts
                </button>
              </div>
            </header>

            {tab === 'summary' ? (
              <RunSummaryPanel cdetsId={detail.cdets_id} reloadKey={detailTick} />
            ) : (
              <>
                <TokenUsagePanel detail={detail} />
                <nav className="file-tabs" aria-label="Artifact files">
                  {detail.files.map((f) => (
                    <button
                      key={f.name}
                      className={`file-tab ${f.name === activeFile ? 'file-tab--active' : ''}`}
                      onClick={() => setActiveFile(f.name)}
                    >
                      {fileIcon(f.kind)} {f.name}
                    </button>
                  ))}
                </nav>
                <div className="viewer">
                  {selectedFile && <FileViewer cdetsId={detail.cdets_id} file={selectedFile} />}
                </div>
              </>
            )}
          </>
        )}
      </section>
    </div>
  );
}
