import { useCallback, useEffect, useState } from "react";

import { EmailDetail } from "./EmailDetail";
import { Inbox } from "./Inbox";
import { Login } from "./Login";
import { Link, useLocation } from "./router";
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
  // Stable, so screens can list it as an effect dependency without refetching.
  const sessionEnded = useCallback(() => {
    setView({ kind: "anonymous", notice: SESSION_ENDED });
  }, []);

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
            <nav>
              <strong>Hotel Anemos</strong> <Link to="/">Inbox</Link>
            </nav>
            <button type="button" onClick={() => void leave()}>
              Log out
            </button>
          </header>
          <main className="page">
            <Screen onUnauthenticated={sessionEnded} />
          </main>
        </>
      );
  }
}

const EMAIL_PATH = /^\/emails\/(\d+)$/;

/** The owner's screens by path: / (inbox, ?status= filter), /emails/<id>. */
function Screen({ onUnauthenticated }: { onUnauthenticated: () => void }) {
  const { pathname } = useLocation();
  if (pathname === "/") return <Inbox onUnauthenticated={onUnauthenticated} />;
  const email = EMAIL_PATH.exec(pathname);
  if (email?.[1] !== undefined) {
    const id = Number(email[1]);
    return <EmailDetail key={id} id={id} onUnauthenticated={onUnauthenticated} />;
  }
  return (
    <p>
      There is no such page. <Link to="/">Back to the inbox</Link>
    </p>
  );
}
