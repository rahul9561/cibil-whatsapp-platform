import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get, patch, post } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { dateTime, label } from "../components/format";
import {
  Badge,
  Button,
  Card,
  DataTable,
  ErrorBox,
  Field,
  Input,
  Modal,
  PageHeader,
  Pagination,
  Select,
  Spinner,
} from "../components/ui";
import { useAction } from "../components/useAction";
import { useList } from "../components/useList";

const ROLES = [
  ["agent", "Follow-up Agent"],
  ["admin", "Admin"],
  ["finance", "Finance"],
  ["auditor", "Auditor (read-only)"],
  ["super_admin", "Super Admin"],
];

export function Staff() {
  const { session } = useAuth();
  const list = useList("users");
  const action = useAction(["users"]);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ email: "", first_name: "", last_name: "", role: "agent", password: "" });
  const [editing, setEditing] = useState(null);

  return (
    <>
      <PageHeader
        title="Staff"
        actions={
          <Button
            onClick={() => {
              action.clearError();
              setCreating(true);
            }}
          >
            Add staff member
          </Button>
        }
      />
      <Card>
        <ErrorBox error={list.error || (!creating && !editing && action.error)} />
        {list.isLoading ? (
          <Spinner />
        ) : (
          <DataTable
            rows={list.rows}
            empty="No staff."
            columns={[
              { key: "email", title: "Email" },
              { key: "name", title: "Name", render: (u) => `${u.first_name} ${u.last_name}`.trim() || "—" },
              { key: "role", title: "Role", render: (u) => label(u.role) },
              {
                key: "is_active",
                title: "Status",
                render: (u) => (
                  <Badge value={u.is_active ? "active" : "cancelled"}>
                    {u.is_active ? "Active" : "Deactivated"}
                  </Badge>
                ),
              },
              {
                key: "two_factor_enrolled",
                title: "2FA",
                render: (u) => (u.two_factor_enrolled ? "Enrolled" : "Not set up"),
              },
              { key: "last_login", title: "Last sign-in", render: (u) => dateTime(u.last_login) },
              {
                key: "actions",
                title: "",
                render: (u) =>
                  u.id !== session.user.id && (
                    <Button
                      variant="ghost"
                      onClick={() => {
                        action.clearError();
                        setEditing({ ...u });
                      }}
                    >
                      Edit
                    </Button>
                  ),
              },
            ]}
          />
        )}
        <Pagination page={list.page} count={list.count} onPage={list.setPage} />
      </Card>

      <Modal
        open={creating}
        title="Add staff member"
        onClose={() => setCreating(false)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setCreating(false)}>
              Cancel
            </Button>
            <Button
              disabled={action.busy}
              onClick={async () => {
                if (await action.run(() => post("users", form))) {
                  setCreating(false);
                  setForm({ ...form, email: "", password: "", first_name: "", last_name: "" });
                }
              }}
            >
              Create
            </Button>
          </>
        }
      >
        <Field label="Email">
          <Input
            type="email"
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
          />
        </Field>
        <div className="grid grid-cols-2 gap-2">
          <Field label="First name">
            <Input
              value={form.first_name}
              onChange={(e) => setForm({ ...form, first_name: e.target.value })}
            />
          </Field>
          <Field label="Last name">
            <Input value={form.last_name} onChange={(e) => setForm({ ...form, last_name: e.target.value })} />
          </Field>
        </div>
        <Field label="Role">
          <Select
            value={form.role}
            onChange={(e) => setForm({ ...form, role: e.target.value })}
            options={ROLES}
          />
        </Field>
        <Field
          label="Temporary password"
          hint="At least 12 characters. Share it securely; 2FA is set up at first sign-in for admin and finance roles."
        >
          <Input
            type="password"
            autoComplete="new-password"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
        </Field>
        <ErrorBox error={action.error} />
      </Modal>

      <Modal
        open={!!editing}
        title={`Edit ${editing?.email || ""}`}
        onClose={() => setEditing(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setEditing(null)}>
              Cancel
            </Button>
            <Button
              disabled={action.busy}
              onClick={async () => {
                const body = {
                  role: editing.role,
                  is_active: editing.is_active,
                  first_name: editing.first_name,
                  last_name: editing.last_name,
                };
                if (await action.run(() => patch(`users/${editing.id}`, body))) setEditing(null);
              }}
            >
              Save
            </Button>
          </>
        }
      >
        {editing && (
          <>
            <Field label="Role">
              <Select
                value={editing.role}
                onChange={(e) => setEditing({ ...editing, role: e.target.value })}
                options={ROLES}
              />
            </Field>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={editing.is_active}
                onChange={(e) => setEditing({ ...editing, is_active: e.target.checked })}
              />
              Active (unticking signs them out everywhere)
            </label>
            <div className="border-t pt-3">
              <Button
                variant="danger"
                disabled={action.busy || !editing.two_factor_enrolled}
                onClick={async () => {
                  if (await action.run(() => post(`users/${editing.id}/reset-2fa`))) setEditing(null);
                }}
              >
                Reset 2FA
              </Button>
              <p className="mt-1 text-xs text-slate-500">
                For a lost phone. They will set up 2FA again at next sign-in.
              </p>
            </div>
            <ErrorBox error={action.error} />
          </>
        )}
      </Modal>
    </>
  );
}

export function Audit() {
  const list = useList("audit-logs");
  const [verify, setVerify] = useState(null);
  const action = useAction();
  const [open, setOpen] = useState(null);
  return (
    <>
      <PageHeader
        title="Audit log"
        subtitle="Append-only; each entry is chained to the previous one by hash."
        actions={
          <Button
            variant="secondary"
            disabled={action.busy}
            onClick={async () => setVerify(await action.run(() => get("audit-logs/verify")))}
          >
            Verify integrity
          </Button>
        }
      />
      {verify && (
        <p
          className={`mb-3 rounded-md p-2 text-sm ${verify.ok ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}
        >
          {verify.ok
            ? `Chain intact: ${verify.entries_checked} entries verified.`
            : `Chain broken at entry #${verify.first_bad_id} — investigate immediately.`}
        </p>
      )}
      <Card>
        <div className="mb-3 grid gap-3 sm:grid-cols-3">
          <Field label="Action starts with">
            <Input
              value={list.filters.action || ""}
              onChange={(e) => list.setFilter("action", e.target.value)}
              placeholder="e.g. refund."
            />
          </Field>
          <Field label="Actor type">
            <Select
              value={list.filters.actor_type || ""}
              onChange={(e) => list.setFilter("actor_type", e.target.value)}
              options={[
                ["", "Any"],
                ["user", "Staff"],
                ["system", "System"],
                ["webhook", "Webhook"],
                ["anonymous", "Anonymous"],
              ]}
            />
          </Field>
          <Field label="Object id">
            <Input
              value={list.filters.object_id || ""}
              onChange={(e) => list.setFilter("object_id", e.target.value.trim())}
            />
          </Field>
        </div>
        <ErrorBox error={list.error || action.error} />
        {list.isLoading ? (
          <Spinner />
        ) : (
          <DataTable
            rows={list.rows}
            empty="No entries."
            onRowClick={(e) => setOpen(open === e.id ? null : e.id)}
            columns={[
              { key: "created_at", title: "When", render: (e) => dateTime(e.created_at) },
              { key: "actor_label", title: "Who", render: (e) => e.actor_label || label(e.actor_type) },
              {
                key: "action",
                title: "Action",
                render: (e) => <span className="font-mono text-xs">{e.action}</span>,
              },
              {
                key: "object_type",
                title: "Object",
                render: (e) => `${e.object_type}${e.object_id ? ` · ${e.object_id.slice(0, 8)}` : ""}`,
              },
              { key: "ip", title: "IP" },
              {
                key: "changes",
                title: "Details",
                render: (e) =>
                  open === e.id ? (
                    <pre className="max-w-lg whitespace-pre-wrap text-xs">
                      {JSON.stringify(e.changes, null, 2)}
                    </pre>
                  ) : (
                    <span className="text-xs text-brand-600">Show</span>
                  ),
              },
            ]}
          />
        )}
        <Pagination page={list.page} count={list.count} onPage={list.setPage} />
      </Card>
    </>
  );
}

export function Settings() {
  const q = useQuery({ queryKey: ["settings"], queryFn: () => get("settings") });
  if (q.isLoading) return <Spinner />;
  if (q.error) return <ErrorBox error={q.error} />;
  const s = q.data;
  const rows = [
    ["Report price", `₹${s.report_price_paise / 100}`],
    ["Report validity", `${s.report_validity_days} days`],
    ["PDF retention", `${s.report_pdf_retention_days} days`],
    ["OTP attempts / resends", `${s.otp_max_attempts} / ${s.otp_max_resends}`],
    ["Payment link expiry", `${s.payment_link_expiry_hours} hours`],
    ["Reminder schedule", s.followup_reminder_offsets_hours.map((h) => `${h}h`).join(", ")],
    ...Object.entries(s.whatsapp_templates).map(([k, v]) => [`Template: ${label(k)}`, v]),
    ...Object.entries(s.adapters).map(([k, v]) => [`Adapter: ${label(k)}`, v || "not configured"]),
  ];
  return (
    <>
      <PageHeader title="Settings" subtitle="Read-only here; values come from the server environment." />
      <Card>
        <dl className="divide-y divide-slate-100 text-sm">
          {rows.map(([k, v]) => (
            <div key={k} className="flex justify-between py-2">
              <dt className="text-slate-500">{k}</dt>
              <dd className="font-medium">{v === "mock" ? <Badge value="pending">mock</Badge> : v}</dd>
            </div>
          ))}
        </dl>
      </Card>
    </>
  );
}
