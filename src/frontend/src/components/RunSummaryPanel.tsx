// Post-run summary panel: bug analysis + scorecard + coverage cards.
// Data comes from GET /api/artifacts/{id}/summary, which reads the
// <id>_summary.json written by the delivery node.

import { useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, FileText, Loader2, XCircle } from 'lucide-react';
import { api } from '@/api';
import type { RunSummary } from '@/types';

interface Props {
  cdetsId: string;
  reloadKey?: number;
}

function gradeTone(grade?: string | null): 'ok' | 'warn' | 'bad' | 'muted' {
  if (!grade) return 'muted';
  const g = grade.toUpperCase();
  if (g === 'HIGH' || g === 'A' || g === 'B') return 'ok';
  if (g === 'MEDIUM' || g === 'C' || g === 'D') return 'warn';
  if (g === 'LOW' || g === 'F') return 'bad';
  return 'muted';
}

function verdictTone(v?: string | null): 'ok' | 'warn' | 'bad' | 'muted' {
  if (!v) return 'muted';
  const s = v.toUpperCase();
  if (s.includes('READY FOR AUTOMATION') || s === 'COVERED') return 'ok';
  if (s.includes('REVIEW') || s.includes('CONDITIONAL') || s === 'PARTIAL') return 'warn';
  if (s.includes('NOT READY') || s === 'GAP' || s === 'NOT_COVERED') return 'bad';
  return 'muted';
}

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n == null || Number.isNaN(n)) return '—';
  return `${n.toFixed(digits)}%`;
}

function fmtNum(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return '—';
  return String(n);
}

/** Triggers may be plain strings or structured objects ({name, details}).
 *  Always render a readable string — never "[object Object]". */
function triggerText(t: unknown): string {
  if (typeof t === 'string') return t;
  if (t && typeof t === 'object') {
    const o = t as Record<string, unknown>;
    return String(o.name ?? o.trigger ?? o.details ?? '').trim();
  }
  return '';
}

/** Derive a SHORT coverage status (label + tone) for the card header, so the
 *  long free-text `cafy_verdict` sentence never gets crammed into a pill. */
function coverageStatus(
  cov: RunSummary['coverage'],
): { label: string; tone: 'ok' | 'warn' | 'bad' | 'muted' } {
  if (!cov) return { label: 'N/A', tone: 'muted' };
  const g = (cov.test_coverage_grade ?? '').toUpperCase();
  if (cov.existing_tests_count === 0 && (cov.new_scenarios_count ?? 0) > 0) {
    return { label: 'No coverage', tone: 'bad' };
  }
  if (g === 'A' || g === 'B') return { label: 'Covered', tone: 'ok' };
  if (g === 'C' || g === 'D') return { label: 'Partial', tone: 'warn' };
  if (g === 'F') return { label: 'No coverage', tone: 'bad' };
  if (cov.existing_tests_count && cov.existing_tests_count > 0) {
    return { label: 'Covered', tone: 'ok' };
  }
  return { label: 'Reviewed', tone: 'muted' };
}

export function RunSummaryPanel({ cdetsId, reloadKey }: Props) {
  const [data, setData] = useState<RunSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getRunSummary(cdetsId)
      .then((r) => {
        if (!cancelled) setData(r);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [cdetsId, reloadKey]);

  if (loading) {
    return (
      <div className="summary-state">
        <Loader2 size={16} className="spin" /> Loading summary…
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="summary-state summary-state--err">
        <AlertTriangle size={16} /> {error || 'No summary available for this defect.'}
      </div>
    );
  }

  const ba = data.bug_analysis ?? {};
  const sc = data.scorecard ?? {};
  const cov = data.coverage;
  const dl = data.delivery;

  return (
    <section className="summary-grid">
      {/* ─── Bug analysis ─── */}
      <article className="summary-card">
        <header className="summary-card__head">
          <FileText size={14} />
          <h4>Bug analysis</h4>
          {ba.qualification?.completion_status && (
            <span
              className={`summary-pill summary-pill--${
                ba.qualification.completion_status === 'COMPLETE' ? 'ok' : 'warn'
              }`}
            >
              {ba.qualification.completion_status}
            </span>
          )}
        </header>

        <div className="summary-card__body">
          {ba.headline && <p className="summary-headline">{ba.headline}</p>}

          <div className="summary-chips">
            {ba.component && (
              <span className="chip">
                <span className="chip__label">Component</span>
                <span className="chip__value">{ba.component}</span>
              </span>
            )}
            {ba.primary_ap && (
              <span className="chip">
                <span className="chip__label">AP</span>
                <span className="chip__value">{ba.primary_ap}</span>
              </span>
            )}
            {ba.severity && (
              <span className="chip">
                <span className="chip__label">Severity</span>
                <span className="chip__value">{ba.severity}</span>
              </span>
            )}
            {ba.version && (
              <span className="chip">
                <span className="chip__label">Version</span>
                <span className="chip__value">{ba.version}</span>
              </span>
            )}
            {ba.failure_category && (
              <span className="chip">
                <span className="chip__label">Failure</span>
                <span className="chip__value">{ba.failure_category}</span>
              </span>
            )}
          </div>

          {ba.repro && (ba.repro.reproducibility || (ba.repro.triggers ?? []).length > 0) && (
            <div className="summary-kv">
              <div className="summary-kv__k">Repro</div>
              <div className="summary-kv__v">
                {ba.repro.reproducibility && <strong>{ba.repro.reproducibility}</strong>}
                {(() => {
                  const labels = (ba.repro.triggers ?? [])
                    .map(triggerText)
                    .filter(Boolean)
                    .slice(0, 2);
                  return labels.length > 0 ? <> · {labels.join(' · ')}</> : null;
                })()}
              </div>
            </div>
          )}

          {ba.behavior?.expected && (
            <div className="summary-kv">
              <div className="summary-kv__k">Expected</div>
              <div className="summary-kv__v">{ba.behavior.expected}</div>
            </div>
          )}
          {ba.behavior?.actual && (
            <div className="summary-kv">
              <div className="summary-kv__k">Actual</div>
              <div className="summary-kv__v">{ba.behavior.actual}</div>
            </div>
          )}

          {ba.rca?.root_cause && (
            <div className="summary-rca">
              <div className="summary-kv__k">
                Root cause
                {ba.rca.confidence && (
                  <span className={`summary-pill summary-pill--${gradeTone(ba.rca.confidence)}`}>
                    {ba.rca.confidence}
                  </span>
                )}
              </div>
              <p>{ba.rca.root_cause}</p>
              {ba.rca.fix_approach && (
                <p className="summary-rca__fix">
                  <em>Fix: </em>
                  {ba.rca.fix_approach}
                </p>
              )}
            </div>
          )}

          {ba.qualification?.missing_fields && ba.qualification.missing_fields.length > 0 && (
            <div className="summary-warn">
              <AlertTriangle size={13} /> Missing: {ba.qualification.missing_fields.join(', ')}
            </div>
          )}
        </div>
      </article>

      {/* ─── Scorecard ─── */}
      <article className="summary-card">
        <header className="summary-card__head">
          <FileText size={14} />
          <h4>Scorecard</h4>
          {sc.grade && (
            <span className={`summary-pill summary-pill--${gradeTone(sc.grade)}`}>{sc.grade}</span>
          )}
        </header>

        <div className="summary-card__body">
          <div className="summary-hero">
            <div className="summary-hero__num">{fmtPct(sc.score_value ?? null, 1)}</div>
            <div className="summary-hero__label">CDETS quality score</div>
          </div>

          <div className="summary-metrics">
            <div className="summary-metric">
              <div className="summary-metric__label">AI confidence</div>
              <div className={`summary-metric__val summary-metric--${gradeTone(sc.ai_confidence?.grade)}`}>
                {fmtPct(sc.ai_confidence?.overall_percent ?? null, 1)}
                {sc.ai_confidence?.grade && (
                  <span className="summary-metric__sub">{sc.ai_confidence.grade}</span>
                )}
              </div>
            </div>

            <div className="summary-metric">
              <div className="summary-metric__label">Automation</div>
              <div
                className={`summary-metric__val summary-metric--${verdictTone(sc.automation_readiness?.verdict)}`}
              >
                {sc.automation_readiness?.verdict ?? '—'}
              </div>
              <div className="summary-metric__sub">
                {fmtNum(sc.automation_readiness?.fields_ready)} ready ·{' '}
                {fmtNum(sc.automation_readiness?.conditional)} cond ·{' '}
                {fmtNum(sc.automation_readiness?.not_ready)} not
              </div>
            </div>

            <div className="summary-metric">
              <div className="summary-metric__label">Required fields</div>
              <div className="summary-metric__val">
                {fmtNum(sc.required_fields?.filled)} / {fmtNum(sc.required_fields?.total)}
              </div>
              <div className="summary-progress">
                <div
                  className="summary-progress__bar"
                  style={{ width: `${Math.min(100, sc.required_fields?.percent ?? 0)}%` }}
                />
              </div>
            </div>
          </div>

          {sc.dt_testability?.alert && (
            <div className="summary-warn">
              <AlertTriangle size={13} /> DT alert: {sc.dt_testability.triggered_count} indicator(s)
              {sc.dt_testability.triggered_list && sc.dt_testability.triggered_list.length > 0 && (
                <> · {sc.dt_testability.triggered_list.slice(0, 3).join(', ')}</>
              )}
            </div>
          )}

          {sc.weakest_fields && sc.weakest_fields.length > 0 && (
            <div className="summary-weakest">
              <div className="summary-kv__k">Weakest fields</div>
              <ul>
                {sc.weakest_fields.map((w) => (
                  <li key={w.path}>
                    <code>{w.path.replace('defect.', '')}</code>
                    {w.label && <span className="taglet">{w.label}</span>}
                    {typeof w.weight === 'number' && <span className="taglet">w={w.weight}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </article>

      {/* ─── Coverage ─── */}
      <article className="summary-card">
        <header className="summary-card__head">
          <FileText size={14} />
          <h4>Coverage</h4>
          {cov && (
            <span className={`summary-pill summary-pill--${coverageStatus(cov).tone}`}>
              {coverageStatus(cov).label}
            </span>
          )}
        </header>

        <div className="summary-card__body">
          {!cov ? (
            <p className="summary-empty">Coverage data unavailable — re-run the analysis.</p>
          ) : (
            <>
              {cov.cafy_verdict && <p className="summary-verdict">{cov.cafy_verdict}</p>}
              <div className="summary-metrics">
                <div className="summary-metric">
                  <div className="summary-metric__label">Coverage confidence</div>
                  <div className={`summary-metric__val summary-metric--${gradeTone(cov.test_coverage_grade)}`}>
                    {fmtPct(cov.test_coverage_confidence ?? null, 1)}
                    {cov.test_coverage_grade && (
                      <span className="summary-metric__sub">{cov.test_coverage_grade}</span>
                    )}
                  </div>
                </div>
                <div className="summary-metric">
                  <div className="summary-metric__label">Existing tests</div>
                  <div className="summary-metric__val">{fmtNum(cov.existing_tests_count)}</div>
                  <div className="summary-metric__sub">
                    {fmtNum(cov.existing_verifiers_count)} verifiers
                  </div>
                </div>
                <div className="summary-metric">
                  <div className="summary-metric__label">New scenarios</div>
                  <div className="summary-metric__val">{fmtNum(cov.new_scenarios_count)}</div>
                  <div className="summary-metric__sub">to author</div>
                </div>
              </div>

              {cov.coverage_gap && (
                <div className="summary-kv">
                  <div className="summary-kv__k">Gap</div>
                  <div className="summary-kv__v">{cov.coverage_gap}</div>
                </div>
              )}
              {cov.gap_classification && (
                <div className="summary-kv">
                  <div className="summary-kv__k">Classification</div>
                  <div className="summary-kv__v">{cov.gap_classification}</div>
                </div>
              )}
            </>
          )}

          {dl && (
            <div className="summary-delivery">
              <span
                className={`summary-pill summary-pill--${dl.mongo_pushed ? 'ok' : 'muted'}`}
                title="MongoDB upsert"
              >
                {dl.mongo_pushed ? <CheckCircle2 size={11} /> : <XCircle size={11} />} Mongo
              </span>
              <span
                className={`summary-pill summary-pill--${dl.tftp_delivered ? 'ok' : 'muted'}`}
                title="TFTP push"
              >
                {dl.tftp_delivered ? <CheckCircle2 size={11} /> : <XCircle size={11} />} TFTP
              </span>
              <span
                className={`summary-pill summary-pill--${dl.email_sent ? 'ok' : 'muted'}`}
                title="Email send"
              >
                {dl.email_sent ? <CheckCircle2 size={11} /> : <XCircle size={11} />} Email
              </span>
              {data.model_used && (
                <span className="summary-pill summary-pill--muted" title="Model used for cdets_tz_analyzer">
                  model: {data.model_used}
                </span>
              )}
            </div>
          )}
        </div>
      </article>
    </section>
  );
}
