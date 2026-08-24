export function Pager({
  page,
  canGoBack,
  canGoForward,
  onBack,
  onForward,
}: {
  page: number;
  canGoBack: boolean;
  canGoForward: boolean;
  onBack: () => void;
  onForward: () => void;
}) {
  if (!canGoBack && !canGoForward) return null;
  return (
    <nav className="pager" aria-label="Results pages">
      <button type="button" onClick={onBack} disabled={!canGoBack}>← Newer</button>
      <span>Page {page}</span>
      <button type="button" onClick={onForward} disabled={!canGoForward}>Older →</button>
    </nav>
  );
}

