import { NavLink, Navigate, Outlet } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { label } from "./format";
import { Spinner } from "./ui";

// Navigation mirrors the API's capability checks; the API remains the enforcement point.
export const NAV = [
  { to: "/", text: "Dashboard", cap: "dashboard.view", end: true },
  { to: "/follow-ups", text: "Follow-ups", cap: "followup.view" },
  { to: "/report-requests", text: "Report requests", cap: "report.view" },
  { to: "/payments", text: "Payments & refunds", cap: "payment.view" },
  { to: "/customers", text: "Customers", cap: "customer.view" },
  { to: "/messages", text: "WhatsApp messages", cap: "customer.view" },
  { to: "/analytics", text: "Revenue", cap: "analytics.revenue" },
  { to: "/staff", text: "Staff", cap: "staff.manage" },
  { to: "/audit", text: "Audit log", cap: "audit.view" },
  { to: "/settings", text: "Settings", cap: "settings.manage" },
];

export function RequireAuth() {
  const { session } = useAuth();
  if (session === undefined) return <Spinner />;
  if (!session) return <Navigate to="/login" replace />;
  if (!session.fully_authenticated) return <Navigate to="/2fa" replace />;
  return <Layout />;
}

function Layout() {
  const { session, can, logout } = useAuth();
  const user = session.user;
  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-56 shrink-0 border-r border-slate-200 bg-white md:block">
        <div className="px-4 py-4 text-sm font-semibold text-brand-700">CIBIL Admin</div>
        <nav className="space-y-0.5 px-2" aria-label="Main">
          {NAV.filter((item) => can(item.cap)).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `block rounded-md px-3 py-2 text-sm ${isActive ? "bg-brand-50 font-medium text-brand-700" : "text-slate-600 hover:bg-slate-50"}`
              }
            >
              {item.text}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-2">
          <nav className="flex gap-2 overflow-x-auto md:hidden" aria-label="Main mobile">
            {NAV.filter((item) => can(item.cap)).map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className="whitespace-nowrap text-xs text-slate-600"
              >
                {item.text}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-sm">
            <span className="text-slate-600">
              {user.email} · <span className="font-medium">{label(user.role)}</span>
            </span>
            <button type="button" onClick={logout} className="text-brand-600 hover:underline">
              Sign out
            </button>
          </div>
        </header>
        <main className="flex-1 p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

export function RequireCap({ cap, children }) {
  const { can } = useAuth();
  if (!can(cap)) return <p className="text-sm text-slate-500">You don't have access to this page.</p>;
  return children;
}
