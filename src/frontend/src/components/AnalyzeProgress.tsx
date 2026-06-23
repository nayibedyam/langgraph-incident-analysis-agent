import { useEffect, useMemo, useRef, useState } from 'react';
import { CheckCircle2, Circle, Loader2, XCircle, MinusCircle } from 'lucide-react';
import { api } from '@/api';
import type {
  HumanInputPayload,
  JobResult,
  MissingInfoRequest,
  ModelChoice,
  NodeEvent,
  NodeStatus,
} from '@/types';
import { HumanReviewForm } from './HumanReviewForm';

// Canonical FL pipeline node order — used to pre-render queued rows so the
// user sees the whole timeline immediately, not just nodes that have started.
const PIPELINE_NODES: { id: string; label: string }[] = [
  { id: 'common_infra', label: 'Common Infra' },
  { id: 'prescan', label: 'Prescan' },
  { id: 'cdets_tz_analyzer', label: 'CDETS + TechZone Analyzer' },
  { id: 'cdets_scoring', label: 'CDETS Scoring' },
  { id: 'missing_info_request', label: 'Missing Info Request (HITL)' },
  { id: 'merge_human_input', label: 'Merge Human Input (HITL)' },
  { id: 'cafy_rca_analyzer', label: 'CaFy RCA Analyzer' },
  { id: 'testcase_generator', label: 'Test Case Generator' },
  { id: 'existing_test_scanner', label: 'Existing Test Scanner' },
  { id: 'merge_coverage', label: 'Merge Coverage' },
  { id: 'coverage_comparison', label: 'Coverage Comparison' },
  { id: 'email_report_generator', label: 'Email Report' },
  { id: 'delivery', label: 'Delivery' },
];

interface Props {
  cdetsId: string;
  model?: ModelChoice | null;
  onDone: (result: JobResult | null) => void;
  onError?: (err: string) => void;
}

function statusIcon(s: NodeStatus | 'queued') {
  switch (s) {
    case 'ok':
      return <CheckCircle2 size={15} className="prog-icon prog-icon--ok" />;
    case 'failed':
      return <XCircle size={15} className="prog-icon prog-icon--bad" />;
    case 'skipped':
      return <MinusCircle size={15} className="prog-icon prog-icon--muted" />;
    case 'running':
      return <Loader2 size={15} className="prog-icon prog-icon--running spin" />;
    default:
      return <Circle size={15} className="prog-icon prog-icon--muted" />;
  }
}

function fmtSecs(s?: number | null) {
  if (s == null || Number.isNaN(s)) return '';
  return `${s.toFixed(1)}s`;
}

function fmtTok(n?: number | null) {
  if (n == null) return '';
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${n}`;
}

/** Live progress panel for one LangGraph pipeline run. */
export function AnalyzeProgress({ cdetsId, model, onDone, onError }: Props) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [events, setEvents] = useState<NodeEvent[]>([]);
  const [status, setStatus] = useState<
    'starting' | 'running' | 'done' | 'error' | 'awaiting_human'
  >('starting');
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [reviewRequest, setReviewRequest] = useState<MissingInfoRequest | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const startRef = useRef<number>(Date.now());
  const esRef = useRef<EventSource | null>(null);
  // Guards against React 18 StrictMode's double-invocation of useEffect in dev,
  // which would otherwise POST /api/jobs twice and trigger two reviewer emails.
  const startedForRef = useRef<string | null>(null);

  // Open (or re-open) the SSE stream for a known job_id. Used both at start
  // and after a `resume` round-trip.
  const openStream = (id: string) => {
    esRef.current?.close();
    const es = new EventSource(api.jobEventsUrl(id));
    esRef.current = es;

    es.onmessage = (ev) => {
      try {
        const data: NodeEvent = JSON.parse(ev.data);
        if (data.node === '__start__' || data.node === '__error__' || data.node === '__resume__') return;
        if (data.node === '__awaiting_human__') {
          setStatus('awaiting_human');
          // Fetch the request payload (it isn't on the event itself).
          api
            .getReview(id)
            .then((env) => setReviewRequest(env.missing_info_request))
            .catch((e) => setError(e instanceof Error ? e.message : String(e)));
          return;
        }
        setEvents((prev) => {
          const without = prev.filter((p) => p.node !== data.node);
          return [...without, data];
        });
      } catch {
        /* ignore malformed frame */
      }
    };
    es.addEventListener('snapshot', (ev: MessageEvent) => {
      try {
        const snap = JSON.parse(ev.data);
        if (Array.isArray(snap.nodes)) {
          setEvents(snap.nodes.filter((n: NodeEvent) => !n.node.startsWith('__')));
        }
        if (snap.status === 'awaiting_human' && snap.missing_info_request) {
          setStatus('awaiting_human');
          setReviewRequest(snap.missing_info_request);
        }
      } catch {
        /* ignore */
      }
    });
    es.addEventListener('done', (ev: MessageEvent) => {
      try {
        const payload = JSON.parse(ev.data);
        if (payload.status === 'awaiting_human') {
          // SSE terminated because the job paused; keep UI in awaiting state.
          es.close();
          esRef.current = null;
          return;
        }
        if (payload.status === 'error') {
          setStatus('error');
          setError(payload.error || 'pipeline failed');
          onError?.(payload.error || 'pipeline failed');
        } else {
          setStatus('done');
          onDone(payload.result ?? null);
        }
      } catch {
        setStatus('done');
        onDone(null);
      } finally {
        es.close();
        esRef.current = null;
      }
    });
    es.onerror = () => {
      // EventSource auto-reconnects; only surface if the job is gone.
      if (esRef.current === es && status !== 'done') {
        /* noop — let it retry */
      }
    };
  };

  // 1. Kick off the job and open the SSE stream.
  useEffect(() => {
    // StrictMode invokes effects twice in dev; only POST once per cdetsId.
    // We intentionally do NOT use a `cancelled` flag or close the EventSource
    // in the cleanup: StrictMode's fake cleanup would fire before the async
    // POST resolves, aborting openStream() and leaving the UI stuck on
    // "Starting analysis…" even though the backend ran the job to completion.
    // Stream lifecycle is instead managed by `openStream` itself (which
    // closes any prior EventSource) and by the 'done' event handler.
    if (startedForRef.current === cdetsId) return;
    startedForRef.current = cdetsId;

    startRef.current = Date.now();
    setStatus('starting');
    setEvents([]);
    setError(null);
    setJobId(null);
    setReviewRequest(null);

    (async () => {
      try {
        const r = await api.startJob(cdetsId, { model: model ?? null });
        setJobId(r.job_id);
        setStatus('running');
        openStream(r.job_id);
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        setStatus('error');
        setError(msg);
        onError?.(msg);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cdetsId]);

  // 2. Per-second elapsed counter (independent of SSE traffic).
  useEffect(() => {
    if (status === 'done' || status === 'error' || status === 'awaiting_human') return;
    const t = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
    }, 1000);
    return () => clearInterval(t);
  }, [status]);

  // 3. Build the rendered row list (canonical order, fill blanks for not-yet-run).
  const rows = useMemo(() => {
    const byName = new Map(events.map((e) => [e.node, e]));
    return PIPELINE_NODES.map(({ id, label }) => {
      const ev = byName.get(id);
      let rowStatus: NodeStatus | 'queued';
      if (!ev) {
        // Determine if it's still pending or actively running. The running node
        // is the first one without a status that follows a completed one.
        rowStatus = 'queued';
      } else {
        rowStatus = ev.status;
      }
      return { id, label, ev, rowStatus };
    });
  }, [events]);

  // Mark the next queued row after the last completed one as "running".
  const visibleRows = useMemo(() => {
    if (status !== 'running') return rows;
    let markedRunning = false;
    return rows.map((r) => {
      if (r.ev) return r;
      if (markedRunning) return r;
      markedRunning = true;
      return { ...r, rowStatus: 'running' as NodeStatus };
    });
  }, [rows, status]);

  const elapsedStr = useMemo(() => {
    const m = Math.floor(elapsed / 60);
    const s = elapsed % 60;
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }, [elapsed]);

  // Running grand-totals across every stage that reported tokens.
  const totals = useMemo(() => {
    let inTok = 0;
    let outTok = 0;
    let any = false;
    for (const ev of events) {
      if (ev.input_tokens != null) {
        inTok += ev.input_tokens;
        any = true;
      }
      if (ev.output_tokens != null) {
        outTok += ev.output_tokens;
        any = true;
      }
    }
    return any ? { inTok, outTok, total: inTok + outTok } : null;
  }, [events]);

  const headerTone =
    status === 'done'
      ? 'progress--done'
      : status === 'error'
        ? 'progress--err'
        : status === 'awaiting_human'
          ? 'progress--hil'
          : '';

  const handleResume = async (payload: HumanInputPayload) => {
    if (!jobId) return;
    setReviewBusy(true);
    try {
      await api.resumeJob(jobId, payload);
      // Move the run back to "running" and re-open the SSE stream so we get
      // the events emitted after the human_review node returns.
      setStatus('running');
      setReviewRequest(null);
      openStream(jobId);
    } finally {
      setReviewBusy(false);
    }
  };

  return (
    <section className={`progress ${headerTone}`}>
      <header className="progress__head">
        <div className="progress__title">
          {status === 'running' || status === 'starting' ? (
            <Loader2 size={15} className="spin" />
          ) : status === 'done' ? (
            <CheckCircle2 size={15} className="prog-icon--ok" />
          ) : status === 'awaiting_human' ? (
            <Circle size={15} className="prog-icon--muted" />
          ) : (
            <XCircle size={15} className="prog-icon--bad" />
          )}
          <span>
            {status === 'starting'
              ? 'Starting analysis…'
              : status === 'running'
                ? `Analyzing ${cdetsId}`
                : status === 'awaiting_human'
                  ? `Waiting for reviewer input: ${cdetsId}`
                  : status === 'done'
                    ? `Analysis complete: ${cdetsId}`
                    : `Analysis failed: ${cdetsId}`}
          </span>
        </div>
        <div className="progress__meta">
          {model && (
            <span className="progress__model" title={`Model selected for cdets_tz_analyzer: ${model}`}>
              {model}
            </span>
          )}
          {totals && (
            <span className="progress__tokens" title="Total tokens used so far">
              <span className="tok tok--in">↑ {fmtTok(totals.inTok)}</span>
              <span className="tok tok--out">↓ {fmtTok(totals.outTok)}</span>
              <span className="tok tok--sum">Σ {fmtTok(totals.total)}</span>
            </span>
          )}
          <span className="progress__elapsed">{elapsedStr}</span>
          {jobId && <span className="progress__jobid">job {jobId}</span>}
        </div>
      </header>

      {error && <div className="progress__error">{error}</div>}

      <ol className="progress__list">
        {visibleRows.map((r) => (
          <li key={r.id} className={`progress__row progress__row--${r.rowStatus}`}>
            <span className="progress__icon">{statusIcon(r.rowStatus)}</span>
            <span className="progress__label">{r.label}</span>
            <span className="progress__stats">
              {r.ev?.duration != null && <span>{fmtSecs(r.ev.duration)}</span>}
              {r.ev?.input_tokens != null && (
                <span className="tok tok--in" title="Input tokens">↑ {fmtTok(r.ev.input_tokens)}</span>
              )}
              {r.ev?.output_tokens != null && (
                <span className="tok tok--out" title="Output tokens">↓ {fmtTok(r.ev.output_tokens)}</span>
              )}
              {r.ev?.iterations != null && r.ev.iterations > 1 && (
                <span>{r.ev.iterations}× iter</span>
              )}
            </span>
            {r.ev?.error && <span className="progress__err-msg">{r.ev.error}</span>}
          </li>
        ))}
      </ol>

      {status === 'awaiting_human' && reviewRequest && jobId && (
        <HumanReviewForm
          jobId={jobId}
          request={reviewRequest}
          onSubmit={handleResume}
        />
      )}
      {status === 'awaiting_human' && !reviewRequest && (
        <div className="progress__error">
          {reviewBusy ? 'Resuming…' : 'Loading reviewer form…'}
        </div>
      )}
    </section>
  );
}
