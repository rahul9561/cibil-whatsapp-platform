import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

// Runs an API call with busy/error state, then refreshes the queries whose keys start with `invalidate`.
export function useAction(invalidate = []) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const run = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      const result = await fn();
      await Promise.all(invalidate.map((key) => queryClient.invalidateQueries({ queryKey: [key] })));
      return result;
    } catch (err) {
      setError(err);
      return undefined;
    } finally {
      setBusy(false);
    }
  };
  return { run, busy, error, clearError: () => setError(null) };
}
