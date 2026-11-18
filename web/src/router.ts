// A minimal router: three screens do not need a library (SPEC, Open items). Paths are
// real URLs (/emails/12), so Back, Forward, reload and bookmarks work; FastAPI serves
// index.html for any UI path (D83), and Vite does the same in development.

import { createElement, type MouseEvent, type ReactNode, useSyncExternalStore } from "react";

const CHANGED = "anemos:navigate";

function subscribe(onChange: () => void): () => void {
  window.addEventListener("popstate", onChange);
  window.addEventListener(CHANGED, onChange);
  return () => {
    window.removeEventListener("popstate", onChange);
    window.removeEventListener(CHANGED, onChange);
  };
}

function snapshot(): string {
  return window.location.pathname + window.location.search;
}

/** The current path and query, re-rendering on every navigation. */
export function useLocation(): URL {
  const current = useSyncExternalStore(subscribe, snapshot);
  return new URL(current, window.location.origin);
}

export function navigate(to: string, { replace = false } = {}): void {
  if (replace) window.history.replaceState(null, "", to);
  else window.history.pushState(null, "", to);
  window.dispatchEvent(new Event(CHANGED));
}

/** An ordinary link that navigates without reloading the page; a click with a
 * modifier key (new tab, new window) is left to the browser. */
export function Link({
  to,
  children,
  className,
}: {
  to: string;
  children: ReactNode;
  className?: string;
}) {
  function follow(event: MouseEvent<HTMLAnchorElement>) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    navigate(to);
  }
  return createElement("a", { href: to, onClick: follow, className }, children);
}
