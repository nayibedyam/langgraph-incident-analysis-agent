import { useEffect, useState } from 'react';
import { Activity, BarChart3, Flame, Layers, RefreshCw, ShieldCheck, Target } from 'lucide-react';
import { api } from '@/api';
import type { Stats } from '@/types';

/** A simple horizontal bar distribution. */
function BarRow({ label, value, max, tone }: { label: string; value: number; max: number; tone?: string }) {
  const pct = max > 0 ? Math.round((value / max) * 100) : 0;
  return (
    <div className="bar-row">
      <span className="bar-row__label">{label}</span>
      <div className="bar-row__track">
        <div className={`bar-row__fill ${tone ? `bar-row__fill--${tone}` : ''}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="bar-row__value">{value}</span>
    </div>
  );
}

function Distribution({
  title,
  icon,
  data,
  toneFor,
}: {
  title: string;
  icon: React.ReactNode;
  data: Record<string, number>;
  toneFor?: (key: string) => string | undefined;
}) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = entries.reduce((m, [, v]) => Math.max(m, v), 0);
  return (
    <section className="panel">
      <h3 className="panel__title">
        {icon} {title}
      </h3>
      {entries.length === 0 ? (
        <p className="panel__empty">No data yet.</p>
      ) : (
        <div className="bars">
          {entries.map(([k, v]) => (
            <BarRow key={k} label={k} value={v} max={max} tone={toneFor?.(k)} />
          ))}
        </div>
      )}
    </section>
  );
}

export function OverviewView({ onOpenDefects }: { onOpenDefects: () => void }) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    api
      .getStats()
      .then(setStats)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  if (loading && !stats) return <div className="view-status">Loading overview…</div>;
  if (error) return <div className="view-status view-status--error">{error}</div>;
  if (!stats) return null;

  const gradeTone = (g: string) => {
    const up = g.toUpperCase();
    if (up.includes('GOOD') || up.includes('STRONG')) return 'ok';
    if (up.includes('MODERATE')) return 'warn';
    if (up.includes('WEAK') || up.includes('POOR')) return 'bad';
    return undefined;
  };
  const coverageTone = (c: string) =>
    c.includes('Fully') ? 'ok' : c.includes('No') ? 'bad' : 'warn';
  const priorityTone = (p: string) =>
    p === 'P1' ? 'bad' : p === 'P2' ? 'warn' : p === 'P3' ? undefined : 'ok';
  const p1p2 = (stats.priority_buckets['P1'] ?? 0) + (stats.priority_buckets['P2'] ?? 0);

  return (
    <div className="overview">
      <div className="overview__head">
        <div>
          <h2 className="overview__title">Feedback Loop Overview</h2>
          <p className="overview__sub">Aggregate metrics across all analyzed CDETS defects.</p>
        </div>
        <button className="btn-ghost" onClick={load} title="Refresh">
          <RefreshCw size={15} /> Refresh
        </button>
      </div>

      <div className="kpis">
        <button className="kpi kpi--clickable" onClick={onOpenDefects}>
          <span className="kpi__icon"><Layers size={18} /></span>
          <span className="kpi__value">{stats.total_defects}</span>
          <span className="kpi__label">Analyzed defects</span>
        </button>
        <div className="kpi">
          <span className="kpi__icon"><BarChart3 size={18} /></span>
          <span className="kpi__value">{stats.avg_cdets_score ?? '—'}{stats.avg_cdets_score != null ? '%' : ''}</span>
          <span className="kpi__label">Avg CDETS score</span>
        </div>
        <div className="kpi">
          <span className="kpi__icon"><ShieldCheck size={18} /></span>
          <span className="kpi__value">{stats.analyzed_with_scores}</span>
          <span className="kpi__label">Scored</span>
        </div>
        <div className="kpi">
          <span className="kpi__icon"><Target size={18} /></span>
          <span className="kpi__value">{stats.coverage_buckets['No Coverage'] ?? 0}</span>
          <span className="kpi__label">No coverage</span>
        </div>
        <div className="kpi">
          <span className="kpi__icon"><Flame size={18} /></span>
          <span className="kpi__value">{p1p2}</span>
          <span className="kpi__label">P1/P2 priority</span>
        </div>
      </div>

      <div className="overview__grid">
        <Distribution title="Triage priority" icon={<Flame size={15} />} data={stats.priority_buckets} toneFor={priorityTone} />
        <Distribution title="Score grades" icon={<BarChart3 size={15} />} data={stats.score_grades} toneFor={gradeTone} />
        <Distribution title="Coverage" icon={<Target size={15} />} data={stats.coverage_buckets} toneFor={coverageTone} />
        <Distribution title="Primary AP" icon={<Layers size={15} />} data={stats.ap_distribution} />
        <Distribution title="Coverage gaps" icon={<Activity size={15} />} data={stats.coverage_gaps} />
      </div>
    </div>
  );
}
