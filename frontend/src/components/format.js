export function rupees(paise) {
  if (paise === null || paise === undefined) return "—";
  const value = paise / 100;
  return `₹${value.toLocaleString("en-IN", { minimumFractionDigits: paise % 100 ? 2 : 0, maximumFractionDigits: 2 })}`;
}

export function dateTime(value) {
  if (!value) return "—";
  return new Date(value).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function relative(value) {
  if (!value) return "—";
  const diff = (new Date(value).getTime() - Date.now()) / 60000;
  const abs = Math.abs(diff);
  const fmt = new Intl.RelativeTimeFormat("en-IN", { numeric: "auto" });
  if (abs < 60) return fmt.format(Math.round(diff), "minute");
  if (abs < 60 * 48) return fmt.format(Math.round(diff / 60), "hour");
  return fmt.format(Math.round(diff / 1440), "day");
}

export function label(value) {
  if (!value) return "—";
  return String(value)
    .replace(/_/g, " ")
    .replace(/^\w/, (c) => c.toUpperCase());
}

// Badge colours by meaning, shared by every status field.
const GOOD = [
  "paid",
  "captured",
  "generated",
  "processed",
  "delivered",
  "read",
  "sent",
  "closed_paid",
  "active",
];
const WARN = [
  "pending",
  "awaiting_payment",
  "otp_sent",
  "fetching",
  "queued",
  "requested",
  "approved",
  "processing",
  "in_progress",
  "open",
  "scheduled",
  "partially_refunded",
  "consented",
  "otp_verified",
  "draft",
];
const BAD = [
  "failed",
  "no_hit",
  "rejected",
  "escalated",
  "cancelled",
  "expired",
  "closed_lost",
  "refunded",
  "fail",
];

export function tone(value) {
  if (GOOD.includes(value)) return "green";
  if (BAD.includes(value)) return "red";
  if (WARN.includes(value)) return "amber";
  return "slate";
}
