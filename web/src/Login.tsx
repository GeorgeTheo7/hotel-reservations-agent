import { type SyntheticEvent, useState } from "react";

import { ApiError, NoAnswer } from "./api";
import { logIn } from "./session";

/** What the owner is told when a login fails. */
export function loginFailure(error: unknown): string {
  if (error instanceof NoAnswer) {
    return "The API did not answer. Is it running on 127.0.0.1:8000?";
  }
  if (error instanceof ApiError) {
    if (error.status === 401) return "Wrong password.";
    if (error.status === 429) {
      const minutes = Math.max(1, Math.ceil((error.retryAfterSeconds ?? 60) / 60));
      const unit = minutes === 1 ? "minute" : "minutes";
      return `Too many wrong passwords. Try again in ${String(minutes)} ${unit}.`;
    }
    return `Could not log in (HTTP ${String(error.status)}).`;
  }
  return "Could not log in.";
}

export function Login({
  onLoggedIn,
  notice,
}: {
  onLoggedIn: () => void;
  notice?: string | undefined;
}) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  async function submit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setFailure(null);
    try {
      await logIn(password);
      setPassword("");
      onLoggedIn();
    } catch (error) {
      setFailure(loginFailure(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <h1>Hotel Anemos</h1>
      <p className="muted">Reservations inbox</p>
      {notice !== undefined && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      <form onSubmit={(event) => void submit(event)}>
        <label htmlFor="password">Owner password</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(event) => {
            setPassword(event.target.value);
          }}
        />
        <button type="submit" disabled={busy || password === ""}>
          {busy ? "Logging in…" : "Log in"}
        </button>
        {failure !== null && (
          <p className="error" role="alert">
            {failure}
          </p>
        )}
      </form>
    </main>
  );
}
