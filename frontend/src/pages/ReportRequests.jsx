import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { get, post } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { dateTime, label, rupees } from "../components/format";
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

const REPORT_STATUSES = [
  "draft",
  "consented",
  "otp_sent",
  "otp_verified",
  "fetching",
  "generated",
  "failed",
  "no_hit",
  "expired",
  "cancelled",
];

export function ReportRequests() {
  const navigate = useNavigate();
  const list = useList("report-requests");
  const { filters, setFilter } = list;
  const columns = [
    {
      key: "public_ref",
      title: "Reference",
      render: (r) => <span className="font-mono">{r.public_ref}</span>,
    },
    { key: "customer_phone", title: "Customer" },
    { key: "report_status", title: "Report", render: (r) => <Badge value={r.report_status} /> },
    {
      key: "payment_status",
      title: "Payment",
      render: (r) => (
        <Badge value={r.payment_status === "awaiting_payment" ? "pending" : r.payment_status}>
          {label(r.payment_status)}
        </Badge>
      ),
    },
    { key: "delivery_status", title: "Delivery", render: (r) => <Badge value={r.delivery_status} /> },
    { key: "price_paise", title: "Price", render: (r) => rupees(r.price_paise) },
    { key: "created_at", title: "Created", render: (r) => dateTime(r.created_at) },
  ];
  return (
    <>
      <PageHeader title="Report requests" />
      <Card>
        <div className="mb-3 grid gap-3 sm:grid-cols-4">
          <Field label="Reference">
            <Input
              placeholder="CR-…"
              value={filters.public_ref || ""}
              onChange={(e) => setFilter("public_ref", e.target.value.trim())}
            />
          </Field>
          <Field label="Report status">
            <Select
              value={filters.report_status || ""}
              onChange={(e) => setFilter("report_status", e.target.value)}
              options={[["", "Any"], ...REPORT_STATUSES.map((s) => [s, label(s)])]}
            />
          </Field>
          <Field label="Payment">
            <Select
              value={filters.unpaid || ""}
              onChange={(e) => setFilter("unpaid", e.target.value)}
              options={[
                ["", "Any"],
                ["true", "Generated, unpaid"],
                ["false", "Paid"],
              ]}
            />
          </Field>
          <Field label="Created from">
            <Input
              type="date"
              value={(filters.created_after || "").slice(0, 10)}
              onChange={(e) =>
                setFilter("created_after", e.target.value ? `${e.target.value}T00:00:00+05:30` : "")
              }
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
            onRowClick={(r) => navigate(`/report-requests/${r.id}`)}
            empty="No report requests found."
          />
        )}
        <Pagination page={list.page} count={list.count} onPage={list.setPage} />
      </Card>
    </>
  );
}

export function ReportRequestDetail() {
  const { id } = useParams();
  const { can } = useAuth();
  const q = useQuery({ queryKey: ["report-requests", id], queryFn: () => get(`report-requests/${id}`) });
  const action = useAction(["report-requests", "follow-ups"]);
  const [cancelling, setCancelling] = useState(false);
  const [reason, setReason] = useState("");
  const [notice, setNotice] = useState("");

  if (q.isLoading) return <Spinner />;
  if (q.error) return <ErrorBox error={q.error} />;
  const r = q.data;
  const cancellable =
    ["draft", "consented", "otp_sent", "otp_verified", "generated"].includes(r.report_status) &&
    r.payment_status !== "paid";

  const rows = [
    [
      "Customer",
      <Link key="c" className="text-brand-600 hover:underline" to={`/customers/${r.customer}`}>
        {r.customer_phone}
      </Link>,
    ],
    ["Report", <Badge key="r" value={r.report_status} />],
    [
      "Payment",
      <Badge key="p" value={r.payment_status === "awaiting_payment" ? "pending" : r.payment_status}>
        {label(r.payment_status)}
      </Badge>,
    ],
    ["Delivery", <Badge key="d" value={r.delivery_status} />],
    ["Price", rupees(r.price_paise)],
    ["Created", dateTime(r.created_at)],
    ["Generated", dateTime(r.generated_at)],
    ["Payable until", dateTime(r.expires_at)],
    ["Delivered", dateTime(r.delivered_at)],
    ["OTP attempts / resends", `${r.otp_attempts} / ${r.otp_resends}`],
    ["Provider", r.provider_name || "—"],
  ];
  if (r.failure_code)
    rows.push(["Failure", `${label(r.failure_code)}${r.failure_detail ? ` — ${r.failure_detail}` : ""}`]);

  return (
    <>
      <PageHeader
        title={<span className="font-mono">{r.public_ref}</span>}
        actions={
          <>
            {can("report.manage") && (
              <Button
                variant="secondary"
                disabled={action.busy}
                onClick={async () => {
                  const res = await action.run(() => post(`report-requests/${id}/retry`));
                  if (res) setNotice(`Queued: ${res.queued}`);
                }}
              >
                Retry stuck step
              </Button>
            )}
            {can("payment.resend_link") && r.report_status === "generated" && r.payment_status !== "paid" && (
              <Button
                variant="secondary"
                disabled={action.busy}
                onClick={async () => {
                  const l = await action.run(() => post(`report-requests/${id}/payment-links/resend`));
                  if (l) setNotice("Payment link sent.");
                }}
              >
                Resend payment link
              </Button>
            )}
            {can("report.download") && r.has_document && (
              <a
                className="inline-flex items-center rounded-md bg-white px-3 py-1.5 text-sm font-medium text-slate-700 ring-1 ring-slate-300 hover:bg-slate-50"
                href={`/api/v1/report-requests/${id}/document`}
              >
                Download PDF
              </a>
            )}
            {can("report.manage") && cancellable && (
              <Button variant="danger" onClick={() => setCancelling(true)}>
                Cancel request
              </Button>
            )}
          </>
        }
      />
      <ErrorBox error={action.error} />
      {notice && <p className="mb-3 rounded-md bg-emerald-50 p-2 text-sm text-emerald-700">{notice}</p>}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Details">
          <dl className="space-y-2 text-sm">
            {rows.map(([k, v]) => (
              <div key={k} className="flex justify-between gap-2">
                <dt className="text-slate-500">{k}</dt>
                <dd className="text-right">{v}</dd>
              </div>
            ))}
          </dl>
          {r.followup && (
            <Link
              to={`/follow-ups/${r.followup.id}`}
              className="mt-4 block text-sm text-brand-600 hover:underline"
            >
              Follow-up: {label(r.followup.status)}
              {r.followup.assigned_to ? ` · ${r.followup.assigned_to}` : ""} →
            </Link>
          )}
        </Card>
        <Card title="Payment links" className="lg:col-span-2">
          <DataTable
            rows={r.payment_links}
            empty="No payment link yet."
            columns={[
              {
                key: "reference_id",
                title: "Reference",
                render: (l) => <span className="font-mono">{l.reference_id}</span>,
              },
              { key: "status", title: "Status", render: (l) => <Badge value={l.status} /> },
              { key: "amount_paise", title: "Amount", render: (l) => rupees(l.amount_paise) },
              { key: "sent_count", title: "Sent" },
              { key: "expires_at", title: "Expires", render: (l) => dateTime(l.expires_at) },
              { key: "paid_at", title: "Paid", render: (l) => dateTime(l.paid_at) },
            ]}
          />
        </Card>
      </div>
      <Modal
        open={cancelling}
        title="Cancel this request?"
        onClose={() => setCancelling(false)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setCancelling(false)}>
              Back
            </Button>
            <Button
              variant="danger"
              disabled={!reason.trim() || action.busy}
              onClick={async () => {
                if (await action.run(() => post(`report-requests/${id}/cancel`, { reason })))
                  setCancelling(false);
              }}
            >
              Cancel request
            </Button>
          </>
        }
      >
        <p className="text-sm text-slate-600">
          Open payment links are cancelled and the follow-up is closed.
        </p>
        <Field label="Reason (required)">
          <Textarea value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <ErrorBox error={action.error} />
      </Modal>
    </>
  );
}
