import { useEffect, useState } from 'react';
import { api } from '@/api';
import type { ArtifactFile, ArtifactFileContent } from '@/types';
import { Markdown } from './Markdown';

export function FileViewer({ cdetsId, file }: { cdetsId: string; file: ArtifactFile }) {
  const [content, setContent] = useState<ArtifactFileContent | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getArtifactFile(cdetsId, file.name)
      .then((c) => !cancelled && setContent(c))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [cdetsId, file.name]);

  if (loading) return <div className="viewer__status">Loading {file.name}…</div>;
  if (error) return <div className="viewer__status viewer__status--error">{error}</div>;
  if (!content) return null;

  if (content.kind === 'markdown') {
    return (
      <div className="viewer__md">
        <Markdown>{content.content}</Markdown>
      </div>
    );
  }
  return (
    <pre className="viewer__code">
      <code>{content.content}</code>
    </pre>
  );
}
