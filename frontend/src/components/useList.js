import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { get } from "../api/client";

// List state (filters + page) lives in the URL, so views are shareable and survive a refresh.
// `privateParams` (e.g. a phone number or PAN being searched) are sent with the request but never put in the
// address bar, so personal data stays out of browser history and server access logs' referrers.
export function useList(path, defaults = {}, privateParams = {}) {
  const [params, setParams] = useSearchParams();
  const filters = { ...defaults, ...Object.fromEntries(params.entries()) };
  const page = Number(filters.page || 1);

  const query = useQuery({
    queryKey: [path, filters, privateParams],
    queryFn: () => get(path, { ...filters, ...privateParams }),
    placeholderData: (previous) => previous,
  });

  // Apply several filters in one URL update (separate updates would each start from the old URL).
  const setFilters = (changes) => {
    const next = new URLSearchParams(params);
    Object.entries(changes).forEach(([key, value]) => {
      if (value === "" || value === undefined || value === null) next.delete(key);
      else next.set(key, value);
    });
    next.delete("page");
    setParams(next, { replace: true });
  };
  const setFilter = (key, value) => setFilters({ [key]: value });
  const setPage = (p) => {
    const next = new URLSearchParams(params);
    next.set("page", String(p));
    setParams(next, { replace: true });
  };

  return {
    ...query,
    filters,
    page,
    setFilter,
    setFilters,
    setPage,
    rows: query.data?.results || [],
    count: query.data?.count,
  };
}
