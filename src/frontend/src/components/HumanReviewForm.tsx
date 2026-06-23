import { useMemo, useState } from 'react';
import { AlertTriangle, Loader2, Send } from 'lucide-react';
import type { HumanInputPayload, MissingFieldRequest, MissingInfoRequest } from '@/types';

interface Props {
  jobId: string;
  request: MissingInfoRequest;
  onSubmit: (payload: HumanInputPayload) => Promise<void>;
  onCancel?: () => void;
}

/** Inline form rendered when a pipeline run pauses on `human_review`. */
export function HumanReviewForm({ jobId, request, onSubmit, onCancel }: Props) {
  const fields: MissingFieldRequest[] = useMemo(
    () => request.missing_fields ?? [],
    [request],
  );
  const questions = useMemo(() => request.free_form_questions ?? [], [request]);

  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [answers, setAnswers] = useState<string[]>(() => questions.map(() => ''));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const updateField = (key: string, val: string) =>
    setFieldValues((prev) => ({ ...prev, [key]: val }));

  const updateAnswer = (idx: number, val: string) =>
    setAnswers((prev) => {
      const next = [...prev];
      next[idx] = val;
      return next;
    });

  const handleSubmit = async (ev: React.FormEvent) => {
    ev.preventDefault();
    if (submitting) return;
    setError(null);

    const cleanedFields: Record<string, string> = {};
    for (const f of fields) {
      const v = (fieldValues[f.field] ?? '').trim();
      if (v) cleanedFields[f.field] = v;
    }
    const cleanedAnswers = answers
      .map((a, i) => (a.trim() ? `${questions[i]}\nA: ${a.trim()}` : ''))
      .filter(Boolean);

    if (Object.keys(cleanedFields).length === 0 && cleanedAnswers.length === 0) {
      setError('Please fill in at least one field or answer one question.');
      return;
    }

    setSubmitting(true);
    try {
      await onSubmit({
        fields: cleanedFields,
        free_form_answers: cleanedAnswers,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSubmitting(false);
    }
  };

  return (
    <section className="hil">
      <header className="hil__head">
        <AlertTriangle size={16} className="hil__icon" />
        <div>
          <div className="hil__title">Reviewer input requested</div>
          <div className="hil__subtitle">
            {request.cdets_id} scored{' '}
            <strong>
              {typeof request.cdet_ai_score === 'number'
                ? request.cdet_ai_score.toFixed(0)
                : '—'}
            </strong>{' '}
            (threshold {request.score_threshold ?? 60}). Provide the missing
            details below so the pipeline can resume.
          </div>
        </div>
      </header>

      {request.summary_for_reviewer && (
        <p className="hil__summary">{request.summary_for_reviewer}</p>
      )}

      <form className="hil__form" onSubmit={handleSubmit}>
        {fields.length > 0 && (
          <div className="hil__section">
            <div className="hil__section-title">Missing fields</div>
            {fields.map((f) => (
              <div key={f.field} className="hil__field">
                <label htmlFor={`hil-${f.field}`}>
                  <span className="hil__field-label">
                    {f.label || f.field}
                  </span>
                  <code className="hil__field-key">{f.field}</code>
                </label>
                {f.why_needed && (
                  <div className="hil__field-why">{f.why_needed}</div>
                )}
                {renderInput(f, fieldValues[f.field] ?? '', (v) => updateField(f.field, v))}
                {f.example && (
                  <div className="hil__field-example">
                    e.g. <em>{f.example}</em>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {questions.length > 0 && (
          <div className="hil__section">
            <div className="hil__section-title">Clarifying questions</div>
            {questions.map((q, idx) => (
              <div key={idx} className="hil__field">
                <label htmlFor={`hil-q-${idx}`}>
                  <span className="hil__field-label">{q}</span>
                </label>
                <textarea
                  id={`hil-q-${idx}`}
                  rows={2}
                  value={answers[idx]}
                  onChange={(e) => updateAnswer(idx, e.target.value)}
                />
              </div>
            ))}
          </div>
        )}

        {error && <div className="hil__error">{error}</div>}

        <div className="hil__actions">
          {onCancel && (
            <button
              type="button"
              className="hil__btn hil__btn--ghost"
              onClick={onCancel}
              disabled={submitting}
            >
              Dismiss
            </button>
          )}
          <button
            type="submit"
            className="hil__btn hil__btn--primary"
            disabled={submitting}
          >
            {submitting ? (
              <>
                <Loader2 size={14} className="spin" /> Resuming…
              </>
            ) : (
              <>
                <Send size={14} /> Submit &amp; resume (job {jobId})
              </>
            )}
          </button>
        </div>
      </form>
    </section>
  );
}

function renderInput(
  f: MissingFieldRequest,
  value: string,
  onChange: (v: string) => void,
) {
  const id = `hil-${f.field}`;
  if (f.input_type === 'select' && Array.isArray(f.options) && f.options.length > 0) {
    return (
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">— pick one —</option>
        {f.options.map((opt) => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
    );
  }
  if (f.input_type === 'text') {
    return (
      <input
        id={id}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <textarea
      id={id}
      rows={4}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}
