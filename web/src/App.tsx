import { useEffect, useState } from "react";

import { Login } from "./Login";
import { logOut, sessionIsLive } from "./session";

type View =
  | { kind: "checking" }
  | { kind: "anonymous"; notice?: string }
  | { kind: "owner" }
  | { kind: "failed" };

// Shown when the API accepted the password but the browser did not send the cookie
// back: most likely a browser that refuses a Secure cookie on http://127.0.0.1.
export const COOKIE_NOT_KEPT =
  "The password was accepted, but the browser did not send the session cookie back. " +
  "On http://127.0.0.1 set ANEMOS_COOKIE_SECURE=false in .env and restart the API (README).";

export const SESSION_ENDED = "Your session has ended. Log in again.";

export function App() {
  const [view, setView] = useState<View>({ kind: "checking" });

  useEffect(() => {
    let current = true;
    sessionIsLive().then(
      (live) => {
        if (current) setView(live ? { kind: "owner" } : { kind: "anonymous" });
      },
      () => {
        if (current) setView({ kind: "failed" });
      },
    );
    return () => {
      current = false;
    };
  }, []);

  async function confirmLogin() {
    // The login answered 204; only a request that carries the cookie proves the
    // browser kept it (D82's Secure cookie on http://127.0.0.1, NOTES).
    try {
      setView((await sessionIsLive()) ? { kind: "owner" } : { kind: "anonymous", notice: COOKIE_NOT_KEPT });
    } catch {
      setView({ kind: "failed" });
    }
  }

  async function leave() {
    try {
      await logOut();
      setView({ kind: "anonymous" });
    } catch {
      setView({ kind: "failed" });
    }
  }

  switch (view.kind) {
    case "checking":
      return <p className="muted center">Loading…</p>;
    case "failed":
      return (
        <p className="error center" role="alert">
          The API did not answer. Is it running on 127.0.0.1:8000? Reload to try again.
        </p>
      );
    case "anonymous":
      return <Login notice={view.notice} onLoggedIn={() => void confirmLogin()} />;
    case "owner":
      return (
        <>
          <header className="bar">
            <strong>Hotel Anemos</strong>
            <button type="button" onClick={() => void leave()}>
              Log out
            </button>
          </header>
          <main className="page">
            <p>Logged in. The inbox is the next screen to build.</p>
          </main>
        </>
      );
  }
}
