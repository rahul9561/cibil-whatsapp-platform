import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockApi, page, renderApp, respond, session } from "./helpers";

describe("sign-in", () => {
  it("goes login -> 2FA -> dashboard", async () => {
    let me = respond(403, { detail: "Authentication required." });
    const api = mockApi({
      "GET auth/me": () => me,
      "POST auth/login": () => session({ verified: false }),
      "POST auth/2fa/verify": () => (me = session()),
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Email"), "me@example.com");
    await user.type(screen.getByLabelText("Password"), "a-long-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await user.type(await screen.findByLabelText(/6-digit code/), "123456");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(api.calls.find((c) => c.path === "auth/2fa/verify").body).toEqual({ token: "123456" });
  });

  it("shows QR enrolment when 2FA is required but not set up", async () => {
    mockApi({
      "GET auth/me": session({ verified: false, enrolled: false }),
      "POST auth/2fa/setup": { otpauth_url: "otpauth://totp/CIBIL:me?secret=ABC" },
    });
    renderApp("/");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Start setup" }));
    expect(await screen.findByText(/Scan this code/)).toBeInTheDocument();
  });

  it("shows the server's error on a bad password", async () => {
    mockApi({
      "GET auth/me": respond(403, {}),
      "POST auth/login": respond(400, { detail: "Invalid email or password." }),
    });
    renderApp("/login");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Email"), "x@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password.");
  });
});

describe("role-aware navigation", () => {
  it("agents see their tools but not admin pages", async () => {
    mockApi({
      "GET auth/me": session({
        role: "agent",
        caps: [
          "dashboard.view",
          "customer.view",
          "report.view",
          "payment.view",
          "followup.view",
          "followup.manage",
        ],
      }),
    });
    renderApp("/");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByText("Follow-ups")).toBeInTheDocument();
    for (const hidden of ["Staff", "Audit log", "Revenue", "Settings"]) {
      expect(within(nav).queryByText(hidden)).not.toBeInTheDocument();
    }
  });

  it("blocks direct navigation to a page the role can't use", async () => {
    mockApi({ "GET auth/me": session({ role: "agent", caps: ["dashboard.view"] }) });
    renderApp("/staff");
    expect(await screen.findByText(/don't have access/)).toBeInTheDocument();
  });
});

describe("follow-up detail", () => {
  it("adds a note", async () => {
    const notes = [];
    const api = mockApi({
      "GET auth/me": session(),
      "GET follow-ups/f1": {
        id: "f1",
        report_request: "r1",
        report_ref: "CR-ABC",
        customer_phone: "+91 98******10",
        amount_paise: 9900,
        status: "open",
        priority: 2,
        escalation_level: 0,
        assigned_to: null,
        closed_at: null,
      },
      "GET follow-ups/f1/notes": () => notes,
      "GET follow-ups/f1/reminders": [],
      "GET follow-ups/assignees": [{ id: "u1", email: "agent@example.com", role: "agent" }],
      "POST follow-ups/f1/notes": ({ body }) => {
        const note = { id: "n1", author: "me@example.com", created_at: new Date().toISOString(), ...body };
        notes.push(note);
        return note;
      },
    });
    renderApp("/follow-ups/f1");
    const user = userEvent.setup();
    await user.type(await screen.findByPlaceholderText(/What happened/), "Called, will pay tonight");
    await user.click(screen.getByRole("button", { name: "Add note" }));
    expect(await screen.findByText("Called, will pay tonight")).toBeInTheDocument();
    expect(api.calls.find((c) => c.method === "POST").body).toMatchObject({
      body: "Called, will pay tonight",
      channel: "call",
    });
  });
});

describe("refunds", () => {
  it("never offers approval on your own refund request", async () => {
    mockApi({
      "GET auth/me": session({ email: "me@example.com" }),
      "GET refunds": page([
        {
          id: "a",
          razorpay_payment_id: "pay_1",
          amount_paise: 100,
          reason: "mine",
          status: "requested",
          requested_by: "me@example.com",
        },
        {
          id: "b",
          razorpay_payment_id: "pay_2",
          amount_paise: 200,
          reason: "theirs",
          status: "requested",
          requested_by: "other@example.com",
        },
      ]),
    });
    renderApp("/payments?tab=refunds");
    expect(await screen.findByText("Needs another approver")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Approve" })).toHaveLength(1);
  });
});

describe("customers", () => {
  it("requires a reason before revealing PII", async () => {
    const api = mockApi({
      "GET auth/me": session(),
      "GET customers/c1": {
        id: "c1",
        phone: "+91 98******10",
        name: "R*** S***",
        pan: "AB******4F",
        email: "",
        gender: "",
        created_at: null,
      },
      "GET customers/c1/timeline": [],
      "POST customers/c1/reveal": { phone: "919876543210" },
    });
    renderApp("/customers/c1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Reveal details…" }));
    const dialog = screen.getByRole("dialog");
    const reveal = within(dialog).getByRole("button", { name: "Reveal" });
    expect(reveal).toBeDisabled();
    await user.type(within(dialog).getByPlaceholderText(/customer called/), "customer called about payment");
    await user.click(reveal);
    expect(await screen.findByText("919876543210")).toBeInTheDocument();
    await waitFor(() =>
      expect(api.calls.find((c) => c.path === "customers/c1/reveal").body).toEqual({
        fields: ["phone"],
        reason: "customer called about payment",
      }),
    );
  });
});
