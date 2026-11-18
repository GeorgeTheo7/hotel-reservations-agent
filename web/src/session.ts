// Log in, check, log out (D82). The cookie itself is HttpOnly: this code never sees
// it, it only learns from the API whether the browser sent it back.

import { ApiError, call } from "./api";

/** True while the browser holds a live session; false on 401. */
export async function sessionIsLive(): Promise<boolean> {
  try {
    await call("GET", "/session");
    return true;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return false;
    throw error;
  }
}

export async function logIn(password: string): Promise<void> {
  await call("POST", "/session", { password });
}

/** Logs out; a session that has already ended (401) counts as logged out. */
export async function logOut(): Promise<void> {
  try {
    await call("DELETE", "/session");
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) throw error;
  }
}
