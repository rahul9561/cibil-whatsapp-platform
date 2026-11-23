import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

// Loaded lazily: the chart library is large and only finance/admin roles ever see revenue.
export default function RevenueChart({ rows }) {
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
