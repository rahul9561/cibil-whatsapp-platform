// Thin fetch wrapper for the admin API. Session cookie auth (same origin) + Django CSRF header on writes.

export class ApiError extends Error {
  constructor(status, body) {
    super(ApiError.describe(status, body));
    this.status = status;
    this.body = body || {};
    this.code = this.body.code;
  }

  static describe(status, body) {
    if (body && typeof body === "object") {
      if (body.detail) return String(body.detail);
      const parts = Object.entries(body).map(([field, msgs]) =>
        field === "non_field_errors" ? [].concat(msgs).join(" ") : `${field}: ${[].concat(msgs).join(" ")}`,
      );
      if (parts.length) return parts.join(" · ");
    }
    if (status === 403) return "You don't have permission to do that.";
    if (status >= 500) return "The server had a problem. Please try again.";
    return `Request failed (${status}).`;
  }
}

export function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export function buildUrl(path, params) {
  const url = `/api/v1/${path.replace(/^\//, "")}`;
  const qs = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    [].concat(value).forEach((v) => qs.append(key, String(v)));
  });
  const query = qs.toString();
  return query ? `${url}?${query}` : url;
}

export async function api(path, { method = "GET", body, params } = {}) {
  const headers = { Accept: "application/json" };
  const unsafe = !["GET", "HEAD", "OPTIONS"].includes(method);
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (unsafe) headers["X-CSRFToken"] = csrfToken();

  const res = await fetch(buildUrl(path, params), {
    method,
    headers,
    credentials: "same-origin",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text.slice(0, 200) };
    }
  }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

export const get = (path, params) => api(path, { params });
export const post = (path, body = {}) => api(path, { method: "POST", body });
export const patch = (path, body) => api(path, { method: "PATCH", body });
export const del = (path) => api(path, { method: "DELETE" });
