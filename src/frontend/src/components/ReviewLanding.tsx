import { useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, Loader2, ShieldCheck } from 'lucide-react';
import { api } from '@/api';
import type { HumanInputPayload, MissingInfoRequest } from '@/types';
import { HumanReviewForm } from './HumanReviewForm';

interface Props {
  jobId: string;
  cdetsHint?: string | null;
}

/** Standalone landing page rendered when the URL contains `?review=<job_id>`.
 *  Reviewers reach this via the deep link in the AI-FL email. It fetches the
 *  pending review payload, renders the form, and POSTs the resume after submit. */
export function ReviewLanding({ jobId, cdetsHint }: Props) {
  const [request, setRequest] = useState<MissingInfoRequest | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getReview(jobId)
      .then((env) => {
        if (cancelled) return;
        setRequest(env.missing_info_request);
      })
      .catch(async (e) => {
        if (cancelled) return;
        // Fall back to the on-disk request payload — useful when the job is
        // no longer in-memory (e.g. after a backend restart) but the file
        // written by missing_info_request_node still exists.
        const cid = cdetsHint ?? null;
        if (cid) {
          try {
            const fallback = await api.getMissingInfo(cid);
            if (!cancelled) setRequest(fallback);
            return;
          } catch {
            /* fall through to the error message below */
          }
        }
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [jobId, cdetsHint]);

  const handleSubmit = async (payload: HumanInputPayload) => {
    await api.resumeJob(jobId, payload);
    setSubmitted(true);
  };

  return (
    <div className="review-landing">
      <header className="review-landing__head">
        <span className="brand__logo">
          <ShieldCheck size={18} />
        </span>
        <div>
          <div className="brand__name">FL Agent — Reviewer Input</div>
          <div className="brand__tag">
            Job {jobId}
            {cdetsHint ? ` · ${cdetsHint}` : ''}
          </div>
        </div>
      </header>

      <main className="review-landing__body">
        {loading && (
          <div className="review-landing__status">
            <Loader2 size={16} className="spin" /> Loading reviewer form…
          </div>
        )}

        {!loading && error && (
          <div className="review-landing__status review-landing__status--err">
            <AlertTriangle size={16} /> {error}
            <div className="review-landing__hint">
              This job may already be resumed, or the backend was restarted.
              Close this tab — the email link is single-use.
            </div>
          </div>
        )}

        {!loading && !error && submitted && (
          <div className="review-landing__status review-landing__status--ok">
            <CheckCircle2 size={16} /> Thanks — your input was submitted and the
            pipeline has resumed. You can close this tab.
          </div>
        )}

        {!loading && !error && !submitted && request && (
          <HumanReviewForm
            jobId={jobId}
            request={request}
            onSubmit={handleSubmit}
          />
        )}
      </main>
    </div>
  );
}
