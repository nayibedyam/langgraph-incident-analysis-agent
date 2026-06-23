import { useEffect, useMemo, useState } from 'react';
import { LayoutDashboard, FileSearch, ShieldCheck } from 'lucide-react';
import { OverviewView } from './components/OverviewView';
import { DefectsView } from './components/DefectsView';
import { ReviewLanding } from './components/ReviewLanding';

type View = 'overview' | 'defects';

/** Parse the inbound URL once at startup. When `?review=<job_id>` is present
 *  the entire app is replaced by the reviewer landing page — that link is what
 *  ships in the AI-FL "input needed" email and must not require navigating the
 *  console UI. */
function parseRoute(): { kind: 'review'; jobId: string; cdets: string | null } | { kind: 'app' } {
  if (typeof window === 'undefined') return { kind: 'app' };
  const params = new URLSearchParams(window.location.search);
  const jobId = params.get('review');
  if (jobId) {
    return { kind: 'review', jobId, cdets: params.get('cdets') };
  }
  return { kind: 'app' };
}

export function App() {
  const route = useMemo(parseRoute, []);
  const [view, setView] = useState<View>('overview');
  const [online, setOnline] = useState<boolean | null>(null);

  useEffect(() => {
    let active = true;
    const ping = () =>
      fetch('/api/health')
        .then((r) => active && setOnline(r.ok))
        .catch(() => active && setOnline(false));
    ping();
    const t = setInterval(ping, 15000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, []);

  if (route.kind === 'review') {
    return <ReviewLanding jobId={route.jobId} cdetsHint={route.cdets} />;
  }

  return (
    <div className="app">
      <header className="app-header">
        <div className="brand">
          <span className="brand__logo">
            <ShieldCheck size={18} />
          </span>
          <div>
            <div className="brand__name">FL Agent Console</div>
            <div className="brand__tag">IOS-XR Feedback Loop</div>
          </div>
        </div>

        <nav className="nav">
          <button
            className={`nav__btn ${view === 'overview' ? 'nav__btn--active' : ''}`}
            onClick={() => setView('overview')}
          >
            <LayoutDashboard size={15} /> Overview
          </button>
          <button
            className={`nav__btn ${view === 'defects' ? 'nav__btn--active' : ''}`}
            onClick={() => setView('defects')}
          >
            <FileSearch size={15} /> Defects
          </button>
        </nav>

        <div className={`status ${online ? 'status--ok' : online === false ? 'status--off' : ''}`}>
          <span className="status__dot" />
          {online ? 'Backend online' : online === false ? 'Backend offline' : 'Connecting…'}
        </div>
      </header>

      <main className="app-main">
        {view === 'overview' ? (
          <OverviewView onOpenDefects={() => setView('defects')} />
        ) : (
          <DefectsView />
        )}
      </main>
    </div>
  );
}
