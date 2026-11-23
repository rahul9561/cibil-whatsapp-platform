import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { get, post } from "../api/client";
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
  Textarea,
} from "../components/ui";
import { useAction } from "../components/useAction";
import { useList } from "../components/useList";

export function Customers() {
  const navigate = useNavigate();
  const list = useList("customers");
  const [phone, setPhone] = useState(list.filters.phone || "");
  const [pan, setPan] = useState(list.filters.pan || "");
  return (
    <>
      <PageHeader
        title="Customers"
        subtitle="Personal details are masked. Search needs the exact phone number or PAN."
      />
      <Card>
        <form
          className="mb-3 flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            list.setFilters({ phone: phone.trim(), pan: pan.trim() });
          }}
        >
          <Field label="Phone">
            <Input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+91 98765 43210" />
          </Field>
          <Field label="PAN">
            <Input value={pan} onChange={(e) => setPan(e.target.value)} placeholder="ABCDE1234F" />
          </Field>
          <Button type="submit">Search</Button>
          <Button
            variant="secondary"
            onClick={() => {
              setPhone("");
              setPan("");
              list.setFilters({ phone: "", pan: "" });
            }}
          >
            Clear
          </Button>
        </form>
        <ErrorBox error={list.error} />
        {list.isLoading ? (
          <Spinner />
        ) : (
          <DataTable
            rows={list.rows}
            onRowClick={(c) => navigate(`/customers/${c.id}`)}
            empty="No customers found."
            columns={[
              { key: "phone", title: "Phone" },
              { key: "name", title: "Name" },
              { key: "pan", title: "PAN" },
              {
                key: "open_request",
                title: "Open request",
                render: (c) => (c.open_request ? <span className="font-mono">{c.open_request}</span> : "—"),
              },
              {
                key: "opted_out_at",
                title: "Reminders",
                render: (c) => (c.opted_out_at ? <Badge value="cancelled">Opted out</Badge> : "On"),
              },
              { key: "created_at", title: "First contact", render: (c) => dateTime(c.created_at) },
            ]}
          />
        )}
        <Pagination page={list.page} count={list.count} onPage={list.setPage} />
      </Card>
    </>
  );
}

const FIELDS = [
  ["phone", "Phone"],
  ["full_name", "Full name"],
  ["pan", "PAN"],
  ["dob", "Date of birth"],
  ["email", "Email"],
  ["pincode", "Pincode"],
];

export function CustomerDetail() {
  const { id } = useParams();
  const { can } = useAuth();
  const customer = useQuery({ queryKey: ["customers", id], queryFn: () => get(`customers/${id}`) });
  const timeline = useQuery({
    queryKey: ["customers", id, "timeline"],
    queryFn: () => get(`customers/${id}/timeline`),
  });
  const action = useAction(["customers", "messages"]);
  const [revealOpen, setRevealOpen] = useState(false);
  const [chosen, setChosen] = useState(["phone"]);
  const [reason, setReason] = useState("");
  const [revealed, setRevealed] = useState(null);
  const [message, setMessage] = useState({ text: "", template_key: "" });

  if (customer.isLoading) return <Spinner />;
  if (customer.error) return <ErrorBox error={customer.error} />;
  const c = customer.data;

  const send = async (e) => {
    e.preventDefault();
    const body = message.template_key
      ? { template_key: message.template_key, template_params: c.open_request ? [c.open_request] : [] }
      : { text: message.text };
    if (await action.run(() => post(`customers/${id}/messages`, body)))
      setMessage({ text: "", template_key: "" });
  };

  return (
    <>
      <PageHeader
        title={c.phone}
        subtitle={`Customer since ${dateTime(c.created_at)}`}
        actions={
          can("customer.reveal_pii") && (
            <Button
              variant="secondary"
              onClick={() => {
                setRevealed(null);
                setRevealOpen(true);
              }}
            >
              Reveal details…
            </Button>
          )
        }
      />
      <ErrorBox error={action.error} />
      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Details">
          <dl className="space-y-2 text-sm">
            {[
              ["Phone", c.phone],
              ["Name", c.name],
              ["PAN", c.pan],
              ["Email", c.email || "—"],
              ["Gender", label(c.gender)],
              ["Reminders", c.opted_out_at ? `Opted out ${dateTime(c.opted_out_at)}` : "On"],
            ].map(([k, v]) => (
              <div key={k} className="flex justify-between gap-2">
                <dt className="text-slate-500">{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
          {revealed && (
            <div className="mt-4 rounded-md bg-amber-50 p-3 text-sm ring-1 ring-amber-200">
              <p className="mb-1 text-xs font-semibold text-amber-800">
                Revealed (logged in the audit trail)
              </p>
              {Object.entries(revealed).map(([k, v]) => (
                <p key={k}>
                  <span className="text-slate-500">{label(k)}:</span> {v || "—"}
                </p>
              ))}
            </div>
          )}
          {can("message.send") && (
            <form onSubmit={send} className="mt-4 space-y-2 border-t pt-3">
              <p className="text-sm font-medium text-slate-700">Send WhatsApp message</p>
              <Select
                value={message.template_key}
                onChange={(e) => setMessage({ ...message, template_key: e.target.value })}
                options={[
                  ["", "Free text (only within 24 h of their last message)"],
                  ["payment_reminder", "Template: payment reminder"],
                ]}
              />
              {!message.template_key && (
                <Textarea
                  value={message.text}
                  onChange={(e) => setMessage({ ...message, text: e.target.value })}
                  placeholder="Message…"
                />
              )}
              <Button type="submit" disabled={action.busy || (!message.template_key && !message.text.trim())}>
                Send
              </Button>
            </form>
          )}
        </Card>
        <Card title="Timeline" className="lg:col-span-2">
          {timeline.isLoading ? (
            <Spinner />
          ) : (timeline.data || []).length === 0 ? (
            <p className="text-sm text-slate-500">No activity.</p>
          ) : (
            <ol className="space-y-2">
              {timeline.data.map((ev, i) => (
                <li key={i} className="flex gap-3 text-sm">
                  <span className="w-36 shrink-0 text-xs text-slate-500">{dateTime(ev.at)}</span>
                  {ev.kind === "message" && (
                    <span>
                      <Badge value={ev.direction === "in" ? "open" : "sent"}>
                        {ev.direction === "in" ? "Customer" : "Us"}
                      </Badge>{" "}
                      <span className="text-slate-700">
                        {ev.template ? `[template ${ev.template}] ` : ""}
                        {ev.text}
                      </span>
                    </span>
                  )}
                  {ev.kind === "report_request" && (
                    <span>
                      Report request{" "}
                      <Link
                        className="font-mono text-brand-600 hover:underline"
                        to={`/report-requests?public_ref=${ev.ref}`}
                      >
                        {ev.ref}
                      </Link>{" "}
                      <Badge value={ev.report_status} /> <Badge value={ev.delivery_status} />
                    </span>
                  )}
                  {ev.kind === "note" && (
                    <span>
                      <Badge value="slate">Note</Badge> {ev.text}{" "}
                      <span className="text-xs text-slate-500">— {ev.author}</span>
                    </span>
                  )}
                </li>
              ))}
            </ol>
          )}
        </Card>
      </div>
      <Modal
        open={revealOpen}
        title="Reveal personal details"
        onClose={() => setRevealOpen(false)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRevealOpen(false)}>
              Back
            </Button>
            <Button
              disabled={!chosen.length || !reason.trim() || action.busy}
              onClick={async () => {
                const data = await action.run(() =>
                  post(`customers/${id}/reveal`, { fields: chosen, reason }),
                );
                if (data) {
                  setRevealed(data);
                  setRevealOpen(false);
                  setReason("");
                }
              }}
            >
              Reveal
            </Button>
          </>
        }
      >
        <fieldset className="grid grid-cols-2 gap-1 text-sm">
          {FIELDS.map(([key, text]) => (
            <label key={key} className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={chosen.includes(key)}
                onChange={(e) =>
                  setChosen(e.target.checked ? [...chosen, key] : chosen.filter((x) => x !== key))
                }
              />
              {text}
            </label>
          ))}
        </fieldset>
        <Field label="Reason (recorded in the audit log)">
          <Input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. customer called about payment"
          />
        </Field>
      </Modal>
    </>
  );
}
