import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { buildUrl, get } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { label, rupees } from "../components/format";
import {
  Button,
  Card,
  DataTable,
  ErrorBox,
  Field,
  Input,
  PageHeader,
  Select,
  Spinner,
  Stat,
} from "../components/ui";

const isoDay = (d) => d.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
const daysAgo = (n) => isoDay(new Date(Date.now() - n * 86400000));

export function Dashboard() {
  const { can } = useAuth();
  const summary = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => get("dashboard/summary"),
    refetchInterval: 60000,
  });
  const funnel = useQuery({
    queryKey: ["funnel"],
    queryFn: () => get("analytics/funnel", { from: daysAgo(29), to: daysAgo(0) }),
  });
  const revenue = useQuery({
    queryKey: ["revenue", "dashboard"],
    queryFn: () => get("analytics/revenue", { from: daysAgo(29), to: daysAgo(0) }),
    enabled: can("analytics.revenue"),
  });

  if (summary.isLoading) return <Spinner />;
  if (summary.error) return <ErrorBox error={summary.error} />;
  const s = summary.data;
  const stages = funnel.data?.stages || {};
  const started = stages.started || 0;

  return (
    <>
      <PageHeader title="Dashboard" subtitle="Today, in IST" />
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Stat title="Requests started" value={s.today.started} />
        <Stat title="Reports generated" value={s.today.generated} />
        <Stat
          title="Payments"
          value={s.today.payments}
          hint={s.today.revenue_paise !== undefined ? rupees(s.today.revenue_paise) : undefined}
        />
        <Stat title="Delivered" value={s.today.delivered} />
        <Stat title="Failed / no history" value={s.today.failed} />
      </div>
      <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Link to="/report-requests?unpaid=true">
          <Stat title="Unpaid reports" value={s.unpaid.count} hint={rupees(s.unpaid.value_paise)} />
        </Link>
        <Link to="/follow-ups?open=true">
          <Stat title="Open follow-ups" value={s.followups.open} />
        </Link>
        <Link to="/follow-ups?open=true&overdue=true">
          <Stat title="Overdue" value={s.followups.overdue} />
        </Link>
        <Link to="/follow-ups?status=escalated">
          <Stat
            title="Escalated"
            value={s.followups.escalated}
            hint={`${s.followups.unassigned} unassigned`}
          />
        </Link>
      </div>
      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card title="Funnel · last 30 days">
          <ul className="space-y-2 text-sm">
            {Object.entries(stages).map(([stage, n]) => (
              <li key={stage}>
                <div className="flex justify-between">
                  <span>{label(stage)}</span>
                  <span>
                    {n}
                    {started ? ` · ${Math.round((n / started) * 100)}%` : ""}
                  </span>
                </div>
                <div className="mt-1 h-2 rounded bg-slate-100">
                  <div
                    className="h-2 rounded bg-brand-500"
                    style={{ width: `${started ? (n / started) * 100 : 0}%` }}
                  />
                </div>
              </li>
            ))}
          </ul>
        </Card>
        {can("analytics.revenue") && (
          <Card title="Net revenue · last 30 days">
            <RevenueChart rows={revenue.data || []} />
          </Card>
        )}
      </div>
    </>
  );
}

function RevenueChart({ rows }) {
  if (!rows.length) return <p className="text-sm text-slate-500">No payments in this period.</p>;
  return (
    <div className="h-56">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows.map((r) => ({ ...r, net: r.net_paise / 100 }))}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="period" fontSize={11} />
          <YAxis fontSize={11} />
          <Tooltip formatter={(v) => `₹${Number(v).toLocaleString("en-IN")}`} />
          <Bar dataKey="net" fill="#2563eb" name="Net" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function Analytics() {
  const [range, setRange] = useState({ from: daysAgo(29), to: daysAgo(0), granularity: "day" });
  const q = useQuery({ queryKey: ["revenue", range], queryFn: () => get("analytics/revenue", range) });
  const rows = q.data || [];
  const total = rows.reduce(
    (acc, r) => ({
      payments: acc.payments + r.payments,
      gross: acc.gross + r.gross_paise,
      refunds: acc.refunds + r.refunds_paise,
      net: acc.net + r.net_paise,
    }),
    { payments: 0, gross: 0, refunds: 0, net: 0 },
  );

  return (
    <>
      <PageHeader
        title="Revenue"
        actions={
          <a href={buildUrl("analytics/revenue", { ...range, export: "csv" })}>
            <Button variant="secondary">Download CSV</Button>
          </a>
        }
      />
      <Card>
        <div className="mb-4 flex flex-wrap items-end gap-3">
          <Field label="From">
            <Input
              type="date"
              value={range.from}
              onChange={(e) => setRange({ ...range, from: e.target.value })}
            />
          </Field>
          <Field label="To">
            <Input
              type="date"
              value={range.to}
              onChange={(e) => setRange({ ...range, to: e.target.value })}
            />
          </Field>
          <Field label="Group by">
            <Select
              value={range.granularity}
              onChange={(e) => setRange({ ...range, granularity: e.target.value })}
              options={[
                ["day", "Day"],
                ["week", "Week"],
                ["month", "Month"],
              ]}
            />
          </Field>
        </div>
        <ErrorBox error={q.error} />
        <div className="mb-4 grid gap-3 sm:grid-cols-4">
          <Stat title="Payments" value={total.payments} />
          <Stat title="Gross" value={rupees(total.gross)} />
          <Stat title="Refunds" value={rupees(total.refunds)} />
          <Stat title="Net" value={rupees(total.net)} />
        </div>
        {q.isLoading ? (
          <Spinner />
        ) : (
          <>
            <RevenueChart rows={rows} />
            <DataTable
              rows={rows.map((r) => ({ ...r, id: r.period }))}
              empty="No payments in this period."
              columns={[
                { key: "period", title: "Period" },
                { key: "payments", title: "Payments" },
                { key: "gross_paise", title: "Gross", render: (r) => rupees(r.gross_paise) },
                { key: "refunds_paise", title: "Refunds", render: (r) => rupees(r.refunds_paise) },
                { key: "net_paise", title: "Net", render: (r) => rupees(r.net_paise) },
              ]}
            />
          </>
        )}
      </Card>
    </>
  );
}
