export const account = "/_allauth/browser/v1/auth/";

function csrfToken() {
  const prefix = "csrftoken=";
  return document.cookie.split("; ").find(cookie => cookie.startsWith(prefix))?.slice(prefix.length);
}

export async function request(path, method = "GET", data) {
  const headers = {};
  if (method !== "GET") headers["X-CSRFToken"] = csrfToken() || "";
  if (data !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method, headers, credentials: "same-origin",
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  const payload = response.status !== 204 && response.headers.get("content-type")?.includes("application/json")
    ? await response.json() : null;
  return { response, payload };
}

export function showError(response, payload) {
  const messages = payload?.errors?.map(error => error.message).filter(Boolean).join(" ");
  return messages || payload?.code || `Request failed (${response.status}).`;
}
