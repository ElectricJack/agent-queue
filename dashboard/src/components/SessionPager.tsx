export function SessionPager({
  page,
  hasMore,
  loading,
  setPage,
}: {
  page: number;
  hasMore: boolean;
  loading: boolean;
  setPage: (page: number) => void;
}) {
  return (
    <nav aria-label="Session pages" className="flex items-center justify-end gap-3 text-sm">
      <button type="button" onClick={() => setPage(page - 1)} disabled={page === 0 || loading}
        className="text-indigo-300 disabled:text-gray-600">
        Previous
      </button>
      <span className="text-gray-400">Page {page + 1}</span>
      <button type="button" onClick={() => setPage(page + 1)} disabled={!hasMore || loading}
        className="text-indigo-300 disabled:text-gray-600">
        Next
      </button>
    </nav>
  );
}
