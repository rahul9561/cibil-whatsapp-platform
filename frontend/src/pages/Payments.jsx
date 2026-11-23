import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { post } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { dateTime, rupees } from "../components/format";
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

const TABS = [
  ["payments", "Payments"],
  ["refunds", "Refunds"],
  ["payment-links", "Payment links"],
];

export function Payments() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") || "payments";
  return (
    <>
      <PageHeader title="Payments & refunds" />
      <div className="mb-3 flex gap-1 border-b border-slate-200" role="tablist">
        {TABS.map(([key, text]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            type="button"
            onClick={() => setParams({ tab: key })}
            className={`px-3 py-2 text-sm ${tab === key ? "border-b-2 border-brand-600 font-medium text-brand-700" : "text-slate-500"}`}
          >
            {text}
          </button>
        ))}
      </div>
      {tab === "payments" && <PaymentList />}
      {tab === "refunds" && <RefundList />}
      {tab === "payment-links" && <LinkList />}
    </>
  );
}

function PaymentList() {
  const { can } = useAuth();
  const list = useList("payments");
  const action = useAction(["payments", "refunds"]);
  const [target, setTarget] = useState(null);
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");

  const openRefund = (p) => {
    setTarget(p);
    setAmount(String(p.refundable_paise / 100));
    setReason("");
    action.clearError();
  };
  const paise = Math.round(Number(amount) * 100);

  return (
    <Card>
      <div className="mb-3 w-48">
        <Field label="Status">
          <Select
            value={list.filters.status || ""}
            onChange={(e) => list.setFilter("status", e.target.value)}
            options={[
              ["", "Any"],
              ["captured", "Captured"],
              ["partially_refunded", "Partially refunded"],
              ["refunded", "Refunded"],
              ["failed", "Failed"],
            ]}
          />
        </Field>
      </div>
      <ErrorBox error={list.error} />
      {list.isLoading ? (
        <Spinner />
      ) : (
        <DataTable
          rows={list.rows}
          empty="No payments yet."
          columns={[
            {
              key: "report_ref",
              title: "Report",
              render: (p) => <span className="font-mono">{p.report_ref}</span>,
            },
            {
              key: "razorpay_payment_id",
              title: "Razorpay id",
              render: (p) => <span className="font-mono text-xs">{p.razorpay_payment_id}</span>,
            },
            { key: "amount_paise", title: "Amount", render: (p) => rupees(p.amount_paise) },
            {
              key: "amount_refunded_paise",
              title: "Refunded",
              render: (p) => rupees(p.amount_refunded_paise),
            },
            { key: "method", title: "Method" },
            { key: "status", title: "Status", render: (p) => <Badge value={p.status} /> },
            { key: "captured_at", title: "Captured", render: (p) => dateTime(p.captured_at) },
            {
              key: "actions",
              title: "",
              render: (p) =>
                can("refund.request") &&
                p.refundable_paise > 0 && (
                  <Button variant="ghost" onClick={() => openRefund(p)}>
                    Request refund
                  </Button>
                ),
            },
          ]}
        />
      )}
      <Pagination page={list.page} count={list.count} onPage={list.setPage} />
      <Modal
        open={!!target}
        title={`Refund ${target?.report_ref || ""}`}
        onClose={() => setTarget(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setTarget(null)}>
              Back
            </Button>
            <Button
              disabled={action.busy || !reason.trim() || !(paise > 0)}
              onClick={async () => {
                if (
                  await action.run(() =>
                    post(`payments/${target.id}/refunds`, { amount_paise: paise, reason }),
                  )
                )
                  setTarget(null);
              }}
            >
              Request refund
            </Button>
          </>
        }
      >
        <Field
          label="Amount (₹)"
          hint={target ? `Up to ${rupees(target.refundable_paise)} (minus refunds already in progress).` : ""}
        >
          <Input
            type="number"
            min="0.01"
            step="0.01"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
          />
        </Field>
        <Field label="Reason (required)">
          <Textarea value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <p className="text-xs text-slate-500">
          A different person with approval rights must approve it before money moves.
        </p>
        <ErrorBox error={action.error} />
      </Modal>
    </Card>
  );
}

function RefundList() {
  const { can, session } = useAuth();
  const list = useList("refunds");
  const action = useAction(["refunds", "payments"]);
  const [decision, setDecision] = useState(null); // { refund, approve }
  const [note, setNote] = useState("");

  return (
    <Card>
      <div className="mb-3 w-48">
        <Field label="Status">
          <Select
            value={list.filters.status || ""}
            onChange={(e) => list.setFilter("status", e.target.value)}
            options={[
              ["", "Any"],
              ["requested", "Awaiting approval"],
              ["processing", "Processing"],
              ["processed", "Processed"],
              ["rejected", "Rejected"],
              ["failed", "Failed"],
            ]}
          />
        </Field>
      </div>
      <ErrorBox error={list.error || action.error} />
      {list.isLoading ? (
        <Spinner />
      ) : (
        <DataTable
          rows={list.rows}
          empty="No refunds."
          columns={[
            {
              key: "razorpay_payment_id",
              title: "Payment",
              render: (r) => <span className="font-mono text-xs">{r.razorpay_payment_id}</span>,
            },
            { key: "amount_paise", title: "Amount", render: (r) => rupees(r.amount_paise) },
            {
              key: "reason",
              title: "Reason",
              render: (r) => (
                <span className="block max-w-xs truncate" title={r.reason}>
                  {r.reason}
                </span>
              ),
            },
            { key: "status", title: "Status", render: (r) => <Badge value={r.status} /> },
            { key: "requested_by", title: "Requested by" },
            { key: "approved_by", title: "Decided by", render: (r) => r.approved_by || "—" },
            { key: "created_at", title: "Requested", render: (r) => dateTime(r.created_at) },
            {
              key: "actions",
              title: "",
              render: (r) =>
                can("refund.approve") &&
                r.status === "requested" &&
                (r.requested_by === session.user.email ? (
                  <span className="text-xs text-slate-400">Needs another approver</span>
                ) : (
                  <span className="flex gap-1">
                    <Button
                      variant="ghost"
                      onClick={() => {
                        setNote("");
                        setDecision({ refund: r, approve: true });
                      }}
                    >
                      Approve
                    </Button>
                    <Button
                      variant="ghost"
                      onClick={() => {
                        setNote("");
                        setDecision({ refund: r, approve: false });
                      }}
                    >
                      Reject
                    </Button>
                  </span>
                )),
            },
          ]}
        />
      )}
      <Pagination page={list.page} count={list.count} onPage={list.setPage} />
      <Modal
        open={!!decision}
        title={decision?.approve ? "Approve refund" : "Reject refund"}
        onClose={() => setDecision(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDecision(null)}>
              Back
            </Button>
            <Button
              variant={decision?.approve ? "primary" : "danger"}
              disabled={action.busy}
              onClick={async () => {
                const verb = decision.approve ? "approve" : "reject";
                if (await action.run(() => post(`refunds/${decision.refund.id}/${verb}`, { note })))
                  setDecision(null);
              }}
            >
              {decision?.approve ? `Approve ${rupees(decision?.refund.amount_paise)}` : "Reject"}
            </Button>
          </>
        }
      >
        {decision?.approve && (
          <p className="text-sm text-slate-600">The refund is sent to Razorpay immediately after approval.</p>
        )}
        <Field label="Note">
          <Textarea value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </Modal>
    </Card>
  );
}

function LinkList() {
  const list = useList("payment-links");
  return (
    <Card>
      <div className="mb-3 w-48">
        <Field label="Status">
          <Select
            value={list.filters.status || ""}
            onChange={(e) => list.setFilter("status", e.target.value)}
            options={[
              ["", "Any"],
              ["active", "Active"],
              ["paid", "Paid"],
              ["expired", "Expired"],
              ["cancelled", "Cancelled"],
              ["failed", "Failed"],
            ]}
          />
        </Field>
      </div>
      <ErrorBox error={list.error} />
      {list.isLoading ? (
        <Spinner />
      ) : (
        <DataTable
          rows={list.rows}
          empty="No payment links."
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
      )}
      <Pagination page={list.page} count={list.count} onPage={list.setPage} />
    </Card>
  );
}
