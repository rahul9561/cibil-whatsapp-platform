import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { vi } from "vitest";
import { AuthProvider } from "../auth/AuthContext";
import App from "../App";

export const ALL_CAPS = [
  "dashboard.view",
  "customer.view",
  "customer.reveal_pii",
  "report.view",
  "report.manage",
  "report.download",
  "payment.view",
  "payment.resend_link",
  "message.send",
  "followup.view",
  "followup.manage",
  "followup.assign",
  "refund.request",
  "refund.approve",
  "analytics.revenue",
  "staff.manage",
  "audit.view",
  "settings.manage",
];

export function session({
  role = "admin",
  caps = ALL_CAPS,
  verified = true,
  enrolled = true,
  email = "me@example.com",
} = {}) {
  return {
    user: { id: "u-me", email, first_name: "", last_name: "", role, capabilities: caps },
    two_factor: { required: true, enrolled, verified },
    fully_authenticated: verified,
  };
}

export const page = (results) => ({ count: results.length, next: null, previous: null, results });
export const respond = (status, body) => ({ __response: true, status, body });

// handlers: { "GET auth/me": body | respond(status, body) | (req) => either }
export function mockApi(handlers) {
  const calls = [];
  const defaults = {
    "GET auth/csrf": { detail: "ok" },
    "GET dashboard/summary": {
      today: { started: 3, generated: 2, failed: 0, delivered: 1, payments: 1, revenue_paise: 9900 },
      unpaid: { count: 1, value_paise: 9900 },
      followups: { open: 1, overdue: 0, escalated: 0, unassigned: 1 },
    },
    "GET analytics/funnel": { stages: { started: 3, generated: 2, paid: 1 } },
    "GET analytics/revenue": [],
  };
  const all = { ...defaults, ...handlers };
  const fetchMock = vi.fn(async (url, init = {}) => {
    const method = init.method || "GET";
    const path = url.replace(/^\/api\/v1\//, "").split("?")[0];
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ method, path, url, body, headers: init.headers });
    let handler = all[`${method} ${path}`];
    if (typeof handler === "function") handler = handler({ method, path, url, body });
    if (handler === undefined) {
      return new Response(JSON.stringify({ detail: `unmocked ${method} ${path}` }), { status: 404 });
    }
    const status = handler && handler.__response ? handler.status : 200;
    const payload = handler && handler.__response ? handler.body : handler;
    return new Response(status === 204 ? null : JSON.stringify(payload), { status });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls };
}

export function renderApp(path = "/") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
