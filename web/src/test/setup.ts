// Vitest setup: jest-dom's matchers (toBeInTheDocument, toBeDisabled …) and a clean
// page after every test. Tests make no network call: each one replaces fetch (D84).
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/"); // every test starts on the inbox
});
