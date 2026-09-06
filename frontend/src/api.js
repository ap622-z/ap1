/** 与后端 /api 交互的最小客户端。 */

const BASE = "";

function tokenHeader() {
  const token = localStorage.getItem("ap1_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    ...options,
    headers: { "Content-Type": "application/json", ...tokenHeader(), ...(options.headers || {}) },
  });
  let body = null;
  try {
    body = await res.json();
  } catch (e) {
    body = null;
  }
  if (!res.ok) {
    const err = new Error((body && body.error && body.error.detail) || `HTTP ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

export const api = {
  register(nickname) {
    return request("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ nickname: nickname || null }),
    });
  },
  login(nickname, token) {
    return request("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ nickname, token }),
    });
  },
  send(text, clientMessageId) {
    return request("/api/messages", {
      method: "POST",
      body: JSON.stringify({ text, client_message_id: clientMessageId }),
    });
  },
  history(afterSeq = 0) {
    const q = afterSeq ? `?after_seq=${afterSeq}` : "";
    return request(`/api/messages${q}`);
  },
};
