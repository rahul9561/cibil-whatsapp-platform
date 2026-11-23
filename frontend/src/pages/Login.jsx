import { useState } from "react";
import { Navigate } from "react-router-dom";
import { QRCodeSVG } from "qrcode.react";
import { useAuth } from "../auth/AuthContext";
import { Button, ErrorBox, Field, Input } from "../components/ui";

function Shell({ title, children }) {
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-sm rounded-lg bg-white p-6 shadow ring-1 ring-slate-200">
        <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-brand-600">CIBIL Admin</p>
        <h1 className="mb-5 text-lg font-semibold text-slate-900">{title}</h1>
        {children}
      </div>
    </div>
  );
}

export function Login() {
  const { session, login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  if (session?.fully_authenticated) return <Navigate to="/" replace />;
  if (session) return <Navigate to="/2fa" replace />;

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Shell title="Sign in">
      <form onSubmit={submit} className="space-y-4">
        <Field label="Email">
          <Input
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </Field>
        <Field label="Password">
          <Input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </Field>
        <ErrorBox error={error} />
        <Button type="submit" className="w-full" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </Shell>
  );
}

export function TwoFactor() {
  const { session, verify, startSetup, confirmSetup, logout } = useAuth();
  const [token, setToken] = useState("");
  const [otpauth, setOtpauth] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  if (!session) return <Navigate to="/login" replace />;
  if (session.fully_authenticated) return <Navigate to="/" replace />;
  const enrolling = !session.two_factor.enrolled;

  const run = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const codeForm = (onSubmit) => (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        run(() => onSubmit(token));
      }}
      className="space-y-4"
    >
      <Field label="6-digit code from your authenticator app">
        <Input
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="\d{6}"
          maxLength={6}
          value={token}
          onChange={(e) => setToken(e.target.value.replace(/\D/g, ""))}
          required
          autoFocus
        />
      </Field>
      <ErrorBox error={error} />
      <Button type="submit" className="w-full" disabled={busy || token.length !== 6}>
        {busy ? "Checking…" : "Continue"}
      </Button>
    </form>
  );

  return (
    <Shell title={enrolling ? "Set up two-factor authentication" : "Two-factor authentication"}>
      {enrolling && !otpauth && (
        <div className="space-y-4">
          <p className="text-sm text-slate-600">
            Your role requires a second factor. You'll need an authenticator app such as Google Authenticator
            or Microsoft Authenticator.
          </p>
          <ErrorBox error={error} />
          <Button
            className="w-full"
            disabled={busy}
            onClick={() => run(async () => setOtpauth((await startSetup()).otpauth_url))}
          >
            Start setup
          </Button>
        </div>
      )}
      {enrolling && otpauth && (
        <div className="space-y-4">
          <p className="text-sm text-slate-600">
            Scan this code with your authenticator app, then enter the code it shows.
          </p>
          <div className="flex justify-center rounded-md bg-white p-3 ring-1 ring-slate-200">
            <QRCodeSVG value={otpauth} size={180} />
          </div>
          {codeForm(confirmSetup)}
        </div>
      )}
      {!enrolling && codeForm(verify)}
      <button
        type="button"
        onClick={logout}
        className="mt-4 w-full text-center text-sm text-slate-500 hover:underline"
      >
        Sign out
      </button>
    </Shell>
  );
}
