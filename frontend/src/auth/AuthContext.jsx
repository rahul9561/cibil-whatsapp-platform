import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, get, post } from "../api/client";

// session = null (logged out) | { user, two_factor: {required, enrolled, verified}, fully_authenticated }
const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [session, setSession] = useState(undefined); // undefined = still loading
  const queryClient = useQueryClient();

  const refresh = useCallback(async () => {
    try {
      await get("auth/csrf");
      setSession(await get("auth/me"));
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) setSession(null);
      else throw err;
    }
  }, []);

  useEffect(() => {
    refresh().catch(() => setSession(null));
  }, [refresh]);

  const actions = useMemo(
    () => ({
      async login(email, password) {
        await get("auth/csrf");
        const s = await post("auth/login", { email, password });
        await get("auth/csrf"); // session key rotated on login: pick up the new CSRF cookie
        setSession(s);
        return s;
      },
      async verify(token) {
        setSession(await post("auth/2fa/verify", { token }));
      },
      async startSetup() {
        return post("auth/2fa/setup");
      },
      async confirmSetup(token) {
        setSession(await post("auth/2fa/confirm", { token }));
      },
      async logout() {
        try {
          await post("auth/logout");
        } finally {
          queryClient.clear(); // never show the next user cached data
          setSession(null);
        }
      },
    }),
    [queryClient],
  );

  const value = useMemo(() => {
    const caps = new Set(session?.user?.capabilities || []);
    return { session, refresh, can: (cap) => caps.has(cap), ...actions };
  }, [session, refresh, actions]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
