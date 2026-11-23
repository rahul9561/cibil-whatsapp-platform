import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { get, patch, post, del } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { dateTime, label, relative, rupees } from "../components/format";
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
  Textarea,
} from "../components/ui";
import { useAction } from "../components/useAction";
import { useList } from "../components/useList";

const PRIORITY = { 1: "Low", 2: "Normal", 3: "High" };

export function FollowUps() {
  const navigate = useNavigate();
  const list = useList("follow-ups", { open: "true" });
  const { filters, setFilter } = list;

  const columns = [
    { key: "report_ref", title: "Report", render: (r) => <span className="font-mono">{r.report_ref}</span> },
    { key: "customer_phone", title: "Customer" },
    { key: "amount_paise", title: "Amount", render: (r) => rupees(r.amount_paise) },
    { key: "status", title: "Status", render: (r) => <Badge value={r.status} /> },
    {
      key: "assigned_to_email",
      title: "Assigned to",
      render: (r) => r.assigned_to_email || <em className="text-slate-400">Unassigned</em>,
    },
    { key: "priority", title: "Priority", render: (r) => PRIORITY[r.priority] },
    {
      key: "escalation_level",
      title: "Esc.",
      render: (r) => (r.escalation_level ? `L${r.escalation_level}` : "—"),
    },
    { key: "next_reminder_at", title: "Next reminder", render: (r) => relative(r.next_reminder_at) },
    { key: "last_note_at", title: "Last note", render: (r) => relative(r.last_note_at) },
  ];

  return (
    <>
      <PageHeader title="Follow-ups" subtitle="Generated reports that are not paid yet" />
      <Card>
        <div className="mb-3 grid gap-3 sm:grid-cols-4">
          <Field label="Show">
            <Select
              value={filters.open}
              onChange={(e) => setFilter("open", e.target.value)}
              options={[
                ["true", "Open"],
                ["false", "Closed"],
                ["", "All"],
              ]}
            />
          </Field>
          <Field label="Assigned">
            <Select
              value={filters.assignee || ""}
              onChange={(e) => setFilter("assignee", e.target.value)}
              options={[
                ["", "Anyone"],
                ["me", "Me"],
                ["unassigned", "Unassigned"],
              ]}
            />
          </Field>
          <Field label="Overdue">
            <Select
              value={filters.overdue || ""}
              onChange={(e) => setFilter("overdue", e.target.value)}
              options={[
                ["", "Any"],
                ["true", "Overdue only"],
              ]}
            />
          </Field>
          <Field label="Status">
            <Select
              value={filters.status || ""}
              onChange={(e) => setFilter("status", e.target.value)}
              options={[
                ["", "Any"],
                ["open", "Open"],
                ["in_progress", "In progress"],
                ["escalated", "Escalated"],
                ["closed_paid", "Closed — paid"],
                ["closed_lost", "Closed — lost"],
                ["closed_manual", "Closed — manually"],
              ]}
            />
          </Field>
        </div>
        <ErrorBox error={list.error} />
        {list.isLoading ? (
          <Spinner />
        ) : (
          <DataTable
            columns={columns}
            rows={list.rows}
            onRowClick={(r) => navigate(`/follow-ups/${r.id}`)}
            empty="No follow-ups match these filters."
          />
        )}
        <Pagination page={list.page} count={list.count} onPage={list.setPage} />
      </Card>
    </>
  );
}

export function FollowUpDetail() {
  const { id } = useParams();
  const { can } = useAuth();
  const fu = useQuery({ queryKey: ["follow-ups", id], queryFn: () => get(`follow-ups/${id}`) });
  const notes = useQuery({
    queryKey: ["follow-ups", id, "notes"],
    queryFn: () => get(`follow-ups/${id}/notes`),
  });
  const reminders = useQuery({
    queryKey: ["follow-ups", id, "reminders"],
    queryFn: () => get(`follow-ups/${id}/reminders`),
  });
  const assignees = useQuery({
    queryKey: ["assignees"],
    queryFn: () => get("follow-ups/assignees"),
    enabled: can("followup.assign"),
  });
  const action = useAction(["follow-ups"]);
  const [note, setNote] = useState({ body: "", channel: "call", outcome: "" });
  const [when, setWhen] = useState("");
  const [closing, setClosing] = useState(null);
  const [reason, setReason] = useState("");
  const [notice, setNotice] = useState("");

  if (fu.isLoading) return <Spinner />;
  if (fu.error) return <ErrorBox error={fu.error} />;
  const f = fu.data;
  const open = !f.closed_at;
  const manage = can("followup.manage") && open;

  const update = (body) => action.run(() => patch(`follow-ups/${id}`, body));

  return (
    <>
      <PageHeader
        title={
          <>
            Follow-up · <span className="font-mono">{f.report_ref}</span>
          </>
        }
        subtitle={`${f.customer_phone} · ${rupees(f.amount_paise)}`}
        actions={
          <>
            <Link
              to={`/report-requests/${f.report_request}`}
              className="text-sm text-brand-600 hover:underline"
            >
              Open report request →
            </Link>
          </>
        }
      />
      <ErrorBox error={action.error} />
      {notice && <p className="mb-3 rounded-md bg-emerald-50 p-2 text-sm text-emerald-700">{notice}</p>}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Status" className="lg:col-span-1">
          <dl className="space-y-2 text-sm">
            <div className="flex justify-between">
              <dt>Status</dt>
              <dd>
                <Badge value={f.status} />
              </dd>
            </div>
            <div className="flex justify-between">
              <dt>Priority</dt>
              <dd>{PRIORITY[f.priority]}</dd>
            </div>
            <div className="flex justify-between">
              <dt>Escalation</dt>
              <dd>{f.escalation_level ? `Level ${f.escalation_level}` : "None"}</dd>
            </div>
            <div className="flex justify-between">
              <dt>Next reminder</dt>
              <dd>{dateTime(f.next_reminder_at)}</dd>
            </div>
            <div className="flex justify-between">
              <dt>Created</dt>
              <dd>{dateTime(f.created_at)}</dd>
            </div>
            {f.closed_at && (
              <div className="flex justify-between">
                <dt>Closed</dt>
                <dd>{dateTime(f.closed_at)}</dd>
              </div>
            )}
            {f.close_reason && <p className="text-slate-500">{f.close_reason}</p>}
          </dl>
          {can("followup.assign") && open && (
            <div className="mt-4 space-y-3 border-t pt-3">
              <Field label="Assigned to">
                <Select
                  value={f.assigned_to || ""}
                  disabled={action.busy}
                  onChange={(e) => update({ assigned_to: e.target.value || null })}
                  options={[
                    ["", "Unassigned"],
                    ...(assignees.data || []).map((u) => [u.id, `${u.email} (${label(u.role)})`]),
                  ]}
                />
              </Field>
              <Field label="Priority">
                <Select
                  value={String(f.priority)}
                  disabled={action.busy}
                  onChange={(e) => update({ priority: Number(e.target.value) })}
                  options={[
                    ["1", "Low"],
                    ["2", "Normal"],
                    ["3", "High"],
                  ]}
                />
              </Field>
            </div>
          )}
          {!can("followup.assign") && (
            <p className="mt-3 text-sm text-slate-600">Assigned to: {f.assigned_to_email || "nobody"}</p>
          )}
          {manage && (
            <div className="mt-4 flex flex-wrap gap-2 border-t pt-3">
              {f.status === "open" && (
                <Button
                  variant="secondary"
                  disabled={action.busy}
                  onClick={() => update({ status: "in_progress" })}
                >
                  Mark in progress
                </Button>
              )}
              {can("payment.resend_link") && (
                <Button
                  variant="secondary"
                  disabled={action.busy}
                  onClick={async () => {
                    const link = await action.run(() =>
                      post(`report-requests/${f.report_request}/payment-links/resend`),
                    );
                    if (link) setNotice(`Payment link sent (${link.sent_count} time(s) so far).`);
                  }}
                >
                  Resend payment link
                </Button>
              )}
              <Button variant="danger" onClick={() => setClosing("closed_lost")}>
                Close…
              </Button>
            </div>
          )}
        </Card>

        <Card title="Notes" className="lg:col-span-2">
          {manage && (
            <form
              className="mb-4 space-y-2"
              onSubmit={async (e) => {
                e.preventDefault();
                if ((await action.run(() => post(`follow-ups/${id}/notes`, note))) !== undefined)
                  setNote({ ...note, body: "", outcome: "" });
              }}
            >
              <Textarea
                placeholder="What happened on this contact?"
                value={note.body}
                onChange={(e) => setNote({ ...note, body: e.target.value })}
                required
              />
              <div className="flex flex-wrap items-end gap-2">
                <div className="w-36">
                  <Select
                    value={note.channel}
                    onChange={(e) => setNote({ ...note, channel: e.target.value })}
                    options={[
                      ["call", "Phone call"],
                      ["whatsapp", "WhatsApp"],
                      ["other", "Other"],
                    ]}
                  />
                </div>
                <div className="w-48">
                  <Select
                    value={note.outcome}
                    onChange={(e) => setNote({ ...note, outcome: e.target.value })}
                    options={[
                      ["", "Outcome…"],
                      ["no_answer", "No answer"],
                      ["callback", "Asked for callback"],
                      ["promised_to_pay", "Promised to pay"],
                      ["paid_claimed", "Says already paid"],
                      ["not_interested", "Not interested"],
                      ["wrong_number", "Wrong number"],
                      ["other", "Other"],
                    ]}
                  />
                </div>
                <Button type="submit" disabled={action.busy || !note.body.trim()}>
                  Add note
                </Button>
              </div>
            </form>
          )}
          {notes.isLoading ? (
            <Spinner />
          ) : (notes.data || []).length === 0 ? (
            <p className="text-sm text-slate-500">No notes yet.</p>
          ) : (
            <ul className="space-y-3">
              {notes.data.map((n) => (
                <li key={n.id} className="rounded-md bg-slate-50 p-3 text-sm">
                  <p className="whitespace-pre-wrap text-slate-800">{n.body}</p>
                  <p className="mt-1 text-xs text-slate-500">
                    {n.author} · {label(n.channel)}
                    {n.outcome && ` · ${label(n.outcome)}`} · {dateTime(n.created_at)}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Reminders" className="lg:col-span-3">
          {manage && (
            <form
              className="mb-3 flex flex-wrap items-end gap-2"
              onSubmit={async (e) => {
                e.preventDefault();
                if (
                  (await action.run(() =>
                    post(`follow-ups/${id}/reminders`, { scheduled_for: new Date(when).toISOString() }),
                  )) !== undefined
                )
                  setWhen("");
              }}
            >
              <Field label="Schedule a reminder">
                <Input
                  type="datetime-local"
                  value={when}
                  onChange={(e) => setWhen(e.target.value)}
                  required
                />
              </Field>
              <Button type="submit" disabled={action.busy || !when}>
                Schedule
              </Button>
            </form>
          )}
          <DataTable
            rows={reminders.data || []}
            empty="No reminders."
            columns={[
              { key: "scheduled_for", title: "When", render: (r) => dateTime(r.scheduled_for) },
              { key: "kind", title: "Kind", render: (r) => label(r.kind) },
              { key: "status", title: "Status", render: (r) => <Badge value={r.status} /> },
              { key: "skip_reason", title: "Note", render: (r) => label(r.skip_reason) },
              {
                key: "actions",
                title: "",
                render: (r) =>
                  manage &&
                  r.status === "scheduled" && (
                    <Button variant="ghost" onClick={() => action.run(() => del(`reminders/${r.id}`))}>
                      Cancel
                    </Button>
                  ),
              },
            ]}
          />
        </Card>
      </div>

      <Modal
        open={!!closing}
        title="Close follow-up"
        onClose={() => setClosing(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setClosing(null)}>
              Back
            </Button>
            <Button
              variant="danger"
              disabled={!reason.trim() || action.busy}
              onClick={async () => {
                if (await action.run(() => update({ status: closing, close_reason: reason })))
                  setClosing(null);
              }}
            >
              Close follow-up
            </Button>
          </>
        }
      >
        <Field label="Outcome">
          <Select
            value={closing || ""}
            onChange={(e) => setClosing(e.target.value)}
            options={[
              ["closed_lost", "Lost — customer won't pay"],
              ["closed_manual", "Closed for another reason"],
            ]}
          />
        </Field>
        <Field label="Reason (required)">
          <Textarea value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <p className="text-xs text-slate-500">Pending reminders are cancelled. This can't be reopened.</p>
      </Modal>
    </>
  );
}
