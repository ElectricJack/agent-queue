import { useSearchParams } from "react-router-dom";

export function useSessionPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requested = Number(searchParams.get("page") ?? "1");
  const page = Number.isSafeInteger(requested) && requested > 0 && requested <= 10_000
    ? requested - 1
    : 0;

  function setPage(nextPage: number) {
    const next = new URLSearchParams(searchParams);
    if (nextPage === 0) next.delete("page");
    else next.set("page", String(nextPage + 1));
    setSearchParams(next);
  }

  return { page, setPage };
}
