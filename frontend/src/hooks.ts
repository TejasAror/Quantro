import { useCallback, useEffect, useRef, useState, type DependencyList } from "react";
import { QuantroApiError } from "./api";

export type AsyncState<T> = {
  data: T | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<T | null>;
  setData: React.Dispatch<React.SetStateAction<T | null>>;
};

export function usePollingResource<T>(
  loader: () => Promise<T>,
  deps: DependencyList,
  intervalMs = 5000,
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const loaderRef = useRef(loader);
  const resourceVersionRef = useRef(0);

  useEffect(() => {
    loaderRef.current = loader;
  });

  const refresh = useCallback(async (expectedVersion = resourceVersionRef.current) => {
    try {
      setError(null);
      const next = await loaderRef.current();
      if (mountedRef.current && expectedVersion === resourceVersionRef.current) setData(next);
      return next;
    } catch (err) {
      const message = err instanceof QuantroApiError ? err.message : "Unable to load data";
      if (mountedRef.current && expectedVersion === resourceVersionRef.current) setError(message);
      return null;
    } finally {
      if (mountedRef.current && expectedVersion === resourceVersionRef.current) setLoading(false);
    }
    // The caller controls refresh identity through the resource key dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);


    useEffect(() => {
    mountedRef.current = true;
    const version = resourceVersionRef.current + 1;
    resourceVersionRef.current = version;

    let stopped = false;
    let timer: number | undefined;

    const run = async () => {
      if (stopped) return;

      await refresh(version);

      if (!stopped) {
        timer = window.setTimeout(run, intervalMs);
      }
    };

    setLoading(true);
    setData(null);
    void run();

    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
      mountedRef.current = false;
    };
  }, [refresh, intervalMs]);

  return { data, loading, error, refresh, setData };
}
