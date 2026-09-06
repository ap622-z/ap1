<script setup>
import { computed, nextTick, onMounted, ref } from "vue";
import { api } from "./api.js";

const nickname = ref(localStorage.getItem("ap1_nickname") || "");
const token = ref(localStorage.getItem("ap1_token") || "");
const loggedIn = computed(() => !!nickname.value && !!token.value);

const messages = ref([]);
const input = ref("");
const sending = ref(false);
const pendingClientId = ref("");
const listEl = ref(null);

// —— 身份 ——
const freshToken = ref(""); // 注册返回的一次性令牌（仅当次展示）

async function doRegister() {
  const data = await api.register(nickname.value || null);
  nickname.value = data.nickname;
  token.value = data.token;
  localStorage.setItem("ap1_nickname", data.nickname);
  localStorage.setItem("ap1_token", data.token);
  freshToken.value = data.token; // 让用户保存（仅本次展示一次）
  await enter();
}

async function dismissFreshToken() {
  freshToken.value = "";
}

async function doLogin() {
  await api.login(nickname.value.trim(), token.value.trim());
  localStorage.setItem("ap1_nickname", nickname.value.trim());
  localStorage.setItem("ap1_token", token.value.trim());
  await enter();
}

function logout() {
  nickname.value = "";
  token.value = "";
  localStorage.removeItem("ap1_nickname");
  localStorage.removeItem("ap1_token");
  messages.value = [];
}

async function enter() {
  if (!loggedIn.value) return;
  await loadHistory();
}

// —— 历史（逐页拉全，刷新可完整恢复）——
async function loadHistory() {
  let afterSeq = 0;
  const all = [];
  for (;;) {
    const data = await api.history(afterSeq);
    all.push(...data.messages);
    if (!data.has_more || !data.messages.length) break;
    afterSeq = data.next_seq;
  }
  messages.value = all.map((m) => ({
    id: m.id,
    seq: m.seq,
    type: m.type, // user / agent / tool
    text: (m.payload && (m.payload.text || "")) || "",
    tool: m.type === "tool" ? m.payload : null,
    status: m.status,
  }));
  await scrollToBottom();
}

function scrollToBottom() {
  nextTick(() => {
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight;
  });
}

// —— 发送（幂等：每条消息一个 key；失败后“重发”沿用同 key → 服务端命中不重复跑）——
const failedClientId = ref("");
const failedText = ref("");

function newClientId() {
  if ("crypto" in window && crypto.randomUUID) return crypto.randomUUID();
  return "m-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

function clearPendingFail() {
  failedClientId.value = "";
  failedText.value = "";
  pendingClientId.value = "";
}

async function send() {
  const text = input.value.trim();
  if (!text || sending.value) return;
  input.value = "";
  const clientId = newClientId();
  pendingClientId.value = clientId;
  messages.value.push({ id: "local-" + clientId, type: "user", text, tool: null, status: "processing" });
  sending.value = true;
  scrollToBottom();
  try {
    await api.send(text, clientId);
    clearPendingFail();
    await loadHistory(); // 以服务端为准重建列表（占位随之消失）
  } catch (err) {
    // 失败：记录该条，气泡转可重发态
    failedClientId.value = clientId;
    failedText.value = text;
    const item = messages.value.find((m) => m.id === "local-" + clientId);
    if (item) item.status = "error";
    sending.value = false;
    pendingClientId.value = "";
  }
}

async function retryLast() {
  const clientId = failedClientId.value;
  const text = failedText.value;
  if (!clientId || sending.value) return;
  // 沿用同 key 重发：服务端命中已有消息则续跑/复用，不产生第二条 user 行
  sending.value = true;
  try {
    await api.send(text, clientId);
    clearPendingFail();
    await loadHistory();
  } catch {
    // 仍失败：保持可重发态
    sending.value = false;
  }
}

onMounted(() => {
  if (loggedIn.value) enter();
});
</script>

<template>
  <div class="shell">
    <!-- 身份入口 -->
    <div v-if="!loggedIn" class="auth">
      <h1>与偶像聊聊</h1>
      <p class="hint">注册即得属于你的唯一会话；凭昵称 + 令牌登录可恢复历史。</p>
      <div class="row">
        <input v-model="nickname" placeholder="昵称（可留空自动生成）" />
      </div>
      <div class="row">
        <input v-model="token" placeholder="登录令牌" />
      </div>
      <div class="actions">
        <button class="primary" @click="doRegister">注册 / 进入</button>
        <button class="ghost" @click="doLogin">凭令牌登录</button>
      </div>
      <p class="tiny">令牌仅在注册时展示一次，请妥善保存；换浏览器/清缓存后凭「昵称 + 令牌」登录可恢复。</p>
    </div>

    <!-- 会话窗口 -->
    <div v-else class="chat">
      <header>
        <span class="title">偶像 Agent</span>
        <button class="logout" @click="logout">退出</button>
      </header>
      <div ref="listEl" class="list">
        <div
          v-for="m in messages"
          :key="m.id"
          :class="['bubble-row', m.type === 'user' ? 'mine' : m.type === 'tool' ? 'tool' : 'theirs']"
        >
          <div v-if="m.type === 'tool'" class="tool-note">
            <template v-if="m.tool">{{ m.tool.name }} · 已检索</template>
            <template v-else>（工具）</template>
          </div>
          <div v-else class="bubble" :class="{ sending: m.status === 'error' }">
            {{ m.text }}
            <button v-if="m.status === 'error' && m.id === 'local-' + failedClientId" class="retry-btn" @click="retryLast">
              发送未完成，点此重发
            </button>
          </div>
        </div>
        <div v-if="sending" class="bubble-row theirs">
          <div class="bubble typing"><span class="dot" />正在输入…</div>
        </div>
      </div>
      <footer>
        <input
          v-model="input"
          class="composer"
          placeholder="和偶像说点什么…"
          :disabled="sending"
          @keyup.enter="send"
        />
        <button class="send" :disabled="sending || !input.trim()" @click="send">发送</button>
      </footer>
    </div>

    <!-- 一次性令牌提示（仅注册当次展示，覆盖层） -->
    <div v-if="freshToken" class="token-modal" @click="dismissFreshToken">
      <div class="token-card" @click.stop>
        <h2>你的登录令牌（仅显示一次）</h2>
        <code class="token-code">{{ freshToken }}</code>
        <p class="tiny">昵称：{{ nickname }}。令牌已自动保存到本浏览器；请务必再自行备份一份。</p>
        <button class="primary" @click="dismissFreshToken">我已保存，开始聊天</button>
      </div>
    </div>
  </div>
</template>

<style>
.shell {
  height: 100%;
  display: flex;
  flex-direction: column;
  max-width: 720px;
  margin: 0 auto;
}

/* 身份入口 */
.auth {
  margin: auto;
  width: 100%;
  max-width: 380px;
  padding: 24px;
  background: #fff;
  border-radius: 16px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.08);
}
.auth h1 {
  font-size: 20px;
  margin: 0 0 6px;
}
.auth .hint {
  color: var(--muted);
  font-size: 13px;
  margin: 0 0 16px;
  line-height: 1.5;
}
.auth .row {
  margin-bottom: 10px;
}
.auth input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid #d8d8d8;
  border-radius: 8px;
  font-size: 14px;
}
.auth .actions {
  display: flex;
  gap: 8px;
  margin-top: 6px;
}
.auth .tiny {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.5;
  margin: 14px 0 0;
}
button.primary {
  background: #07c160;
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 14px;
  flex: 1;
  cursor: pointer;
}
button.ghost {
  background: #f2f3f5;
  color: #333;
  border: none;
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 14px;
  flex: 1;
  cursor: pointer;
}

/* 会话 */
.chat {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--bg);
}
.chat header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--header);
  color: #fff;
}
.chat header .title {
  font-weight: 600;
}
.chat header .logout {
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.5);
  color: #fff;
  border-radius: 6px;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 12px;
}
.list {
  flex: 1;
  overflow-y: auto;
  padding: 16px 12px;
}
.bubble-row {
  display: flex;
  margin-bottom: 10px;
}
.bubble-row.mine {
  justify-content: flex-end;
}
.bubble-row.theirs {
  justify-content: flex-start;
}
.bubble-row.tool {
  justify-content: center;
}
.bubble {
  max-width: 72%;
  padding: 9px 13px;
  border-radius: 12px;
  font-size: 15px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
}
.mine .bubble {
  background: var(--bubble-mine);
}
.theirs .bubble {
  background: var(--bubble-other);
}
.bubble.sending {
  opacity: 0.75;
}
.retry-btn {
  display: block;
  margin-top: 6px;
  border: none;
  background: #fdecea;
  color: #d93025;
  font-size: 12px;
  border-radius: 6px;
  padding: 3px 8px;
  cursor: pointer;
}

/* 一次性令牌覆盖层 */
.token-modal {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
  padding: 16px;
}
.token-card {
  background: #fff;
  border-radius: 14px;
  padding: 22px;
  max-width: 460px;
  width: 100%;
}
.token-card h2 {
  font-size: 16px;
  margin: 0 0 12px;
}
.token-code {
  display: block;
  background: #f4f4f5;
  border-radius: 8px;
  padding: 12px;
  font-size: 13px;
  word-break: break-all;
  user-select: all;
  margin-bottom: 12px;
}
.tool-note {
  color: var(--muted);
  font-size: 12px;
  background: #f7f7f7;
  border-radius: 8px;
  padding: 3px 10px;
}
.bubble.typing .dot {
  display: inline-block;
}
footer {
  display: flex;
  gap: 8px;
  padding: 10px 12px;
  background: #f7f7f7;
  border-top: 1px solid #e0e0e0;
}
footer .composer {
  flex: 1;
  border: 1px solid #d8d8d8;
  border-radius: 18px;
  padding: 9px 14px;
  font-size: 15px;
  outline: none;
}
footer .send {
  background: #07c160;
  color: #fff;
  border: none;
  border-radius: 18px;
  padding: 0 18px;
  cursor: pointer;
}
footer .send:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
