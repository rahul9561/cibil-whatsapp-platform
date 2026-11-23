import { Link } from "react-router-dom";
import { dateTime, label } from "../components/format";
import {
  Badge,
  Card,
  DataTable,
  ErrorBox,
  Field,
  PageHeader,
  Pagination,
  Select,
  Spinner,
} from "../components/ui";
import { useList } from "../components/useList";

export function Messages() {
  const list = useList("messages");
  const { filters, setFilter } = list;
  return (
    <>
      <PageHeader
        title="WhatsApp messages"
        subtitle="Bodies are stored redacted; OTPs and personal answers are never kept."
      />
      <Card>
        <div className="mb-3 grid gap-3 sm:grid-cols-3">
          <Field label="Direction">
            <Select
              value={filters.direction || ""}
              onChange={(e) => setFilter("direction", e.target.value)}
              options={[
                ["", "Both"],
                ["in", "From customers"],
                ["out", "From us"],
              ]}
            />
          </Field>
          <Field label="Status">
            <Select
              value={filters.status || ""}
              onChange={(e) => setFilter("status", e.target.value)}
              options={[
                ["", "Any"],
                ["queued", "Queued"],
                ["sent", "Sent"],
                ["delivered", "Delivered"],
                ["read", "Read"],
                ["failed", "Failed"],
                ["received", "Received"],
              ]}
            />
          </Field>
          <Field label="Type">
            <Select
              value={filters.type || ""}
              onChange={(e) => setFilter("type", e.target.value)}
              options={[
                ["", "Any"],
                ["text", "Text"],
                ["template", "Template"],
                ["interactive", "Buttons"],
                ["document", "Document"],
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
            empty="No messages."
            columns={[
              { key: "created_at", title: "When", render: (m) => dateTime(m.created_at) },
              { key: "direction", title: "", render: (m) => (m.direction === "in" ? "← In" : "→ Out") },
              { key: "type", title: "Type", render: (m) => label(m.type) },
              {
                key: "body_redacted",
                title: "Message",
                render: (m) => (
                  <span className="block max-w-md truncate" title={m.body_redacted}>
                    {m.template_name ? `[${m.template_name}] ` : ""}
                    {m.body_redacted}
                  </span>
                ),
              },
              { key: "status", title: "Status", render: (m) => <Badge value={m.status} /> },
              { key: "error_code", title: "Error", render: (m) => m.error_code || "—" },
              {
                key: "customer",
                title: "",
                render: (m) =>
                  m.customer && (
                    <Link className="text-brand-600 hover:underline" to={`/customers/${m.customer}`}>
                      Customer →
                    </Link>
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
