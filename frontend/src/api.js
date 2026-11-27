// Small helper around fetch. The backend URL can be set with VITE_API_URL when the
// frontend and backend are deployed on different addresses. No AI keys live here.
const BASE = import.meta.env.VITE_API_URL || "";

export function getToken() {
  return localStorage.getItem("rms_token");
}

export function setToken(token) {
  if (token) localStorage.setItem("rms_token", token);
  else localStorage.removeItem("rms_token");
}

export async function api(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(BASE + "/api" + path, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(getToken() ? { Authorization: "Bearer " + getToken() } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error("Cannot reach the server. Check your connection and try again.");
  }
  let data = {};
  try {
    data = await response.json();
  } catch {
    throw new Error("The server sent an unexpected reply (HTTP " + response.status + ").");
  }
  // Workflow results (even failed ones) carry an "outcome" and are shown as-is.
  if (!response.ok && !data.outcome) {
    const error = new Error(data.error || "Request failed (HTTP " + response.status + ").");
    error.status = response.status;
    throw error;
  }
  return data;
}

export const label = (text) =>
  (text || "").replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
