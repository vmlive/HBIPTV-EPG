"use strict";

const byId = (id) => document.getElementById(id);
const labels = { epg: "EPG", playlist: "M3U", logos: "Logo", channels: "频道" };
const units = { epg: "条节目", playlist: "个频道", logos: "张图片", channels: "个频道" };
const primaryAuth = new Set(["UserID", "mac", "STBID", "password", "IPTV_AUTH_URL", "ip"]);
const settings = byId("settings");
const login = byId("login");
const passwordDialog = byId("password-dialog");
let provider = "";
let csrf = "";
let channels = [];
let changedChannels = new Map();
let settingsChanged = false;
let jobs = {};
let polling = false;

function message(id, value = "", error = false) {
  byId(id).textContent = value;
  byId(id).classList.toggle("error", error);
}

function showLogin() {
  csrf = "";
  provider = "";
  jobs = {};
  channels = [];
  changedChannels.clear();
  settingsChanged = false;
  passwordDialog.close();
  settings.reset();
  byId("auth-main").replaceChildren();
  byId("auth-extra").replaceChildren();
  byId("channel-rows").replaceChildren();
  byId("console").hidden = true;
  byId("login-view").hidden = false;
}

async function api(path, body) {
  const options = {};
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf };
    options.body = JSON.stringify(body);
  }
  const response = await fetch("/api/" + path, options);
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && csrf) {
      showLogin();
      message("login-status", "会话已过期，请重新登录。", true);
    }
    throw new Error(data.error || "请求失败");
  }
  return data;
}

function authInput(name, value, hasPassword) {
  const label = document.createElement("label");
  const input = document.createElement("input");
  label.append(name, input);
  input.name = "auth-" + name;
  input.autocomplete = "off";
  input.maxLength = 4096;
  input.value = value;
  if (name === "password") {
    input.type = "password";
    input.placeholder = hasPassword ? "已设置，留空保留原密码" : "运营商鉴权密码";
  }
  if (name === "IPTV_AUTH_URL") {
    input.type = "url";
    label.classList.add("wide");
  }
  return label;
}

async function loadProvider(name, channelsOnly = false) {
  const info = await api("provider?name=" + name);
  if (provider !== name) return;
  if (!channelsOnly) {
    settings.elements.enabled.checked = info.enabled;
    settings.elements.interface.value = info.interface || "";
    settings.elements.fwmark.value = info.fwmark ?? "";
    settings.elements.playlist_base_url.value = info.playlist_base_url || "";
    const auth = { ...info.auth, password: "" };
    for (const [id, filter] of [["auth-main", true], ["auth-extra", false]]) {
      byId(id).replaceChildren(...Object.entries(auth)
        .filter(([key]) => primaryAuth.has(key) === filter)
        .map(([key, value]) => authInput(key, value, info.has_password)));
    }
  }
  channels = info.channels.map((channel) => ({ ...channel, ...changedChannels.get(channel.id) }));
  renderChannels();
}

function updateJob() {
  const job = jobs[provider];
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.disabled = job?.state === "running";
  });
  if (!job) message("job-status", "当前没有手动任务");
  else if (job.state === "running") message("job-status", labels[job.action] + " 正在抓取…");
  else if (job.state === "error") message("job-status", labels[job.action] + " 失败。 " + job.message, true);
  else {
    let status = labels[job.action] + " 已完成，共 " + job.count + " " + units[job.action] + "。";
    if (job.file) {
      const updated = new Date(job.file.mtime * 1000).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai", hour12: false,
      });
      status += " 已写入 " + job.file.path + "，更新于 " + updated + "（北京时间）。";
    }
    message("job-status", status);
  }
}

async function switchProvider(name, label) {
  if (name === provider) return;
  if ((settingsChanged || changedChannels.size) && !window.confirm("切换运营商会放弃未保存的改动。继续切换吗？")) return;
  changedChannels.clear();
  settingsChanged = false;
  provider = name;
  byId("provider-title").textContent = label + " IPTV";
  byId("channel-filter").value = "";
  for (const id of ["notice", "settings-status", "channel-status"]) message(id);
  for (const button of byId("providers").children) {
    button.setAttribute("aria-pressed", String(button.dataset.provider === name));
  }
  channels = [];
  renderChannels();
  settings.reset();
  byId("auth-main").replaceChildren();
  byId("auth-extra").replaceChildren();
  updateJob();
  try {
    await loadProvider(name);
  } catch (error) {
    if (provider === name) message("notice", error.message, true);
  }
}

async function enterConsole() {
  const session = await api("session");
  csrf = session.csrf;
  message("login-status");
  byId("login-view").hidden = true;
  byId("console").hidden = false;
  byId("providers").replaceChildren();
  for (const [name, label] of Object.entries(session.providers)) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.provider = name;
    button.textContent = label;
    button.onclick = () => switchProvider(name, label);
    byId("providers").append(button);
  }
  const [name, label] = Object.entries(session.providers)[0];
  await switchProvider(name, label);
  await pollJobs();
}

login.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = login.querySelector("button");
  submit.disabled = true;
  message("login-status");
  try {
    await api("login", Object.fromEntries(new FormData(login)));
    login.elements.password.value = "";
    await enterConsole();
  } catch (error) {
    message("login-status", error.message, true);
  } finally {
    submit.disabled = false;
  }
});

byId("logout").onclick = async () => {
  try {
    await api("logout", {});
    showLogin();
  } catch (error) {
    message("notice", error.message, true);
  }
};

byId("password-open").onclick = () => {
  byId("password-form").reset();
  message("password-status");
  passwordDialog.showModal();
};
byId("password-cancel").onclick = () => passwordDialog.close();
byId("password-form").onsubmit = async (event) => {
  event.preventDefault();
  try {
    await api("password", Object.fromEntries(new FormData(event.target)));
    event.target.reset();
    showLogin();
    message("login-status", "密码已修改，请重新登录。");
  } catch (error) {
    message("password-status", error.message, true);
  }
};

settings.addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = provider;
  const fields = new FormData(settings);
  const auth = Object.fromEntries([...fields].filter(([key]) => key.startsWith("auth-"))
    .map(([key, value]) => [key.slice(5), value]));
  const mark = fields.get("fwmark");
  const update = {
    enabled: settings.elements.enabled.checked,
    interface: fields.get("interface").trim() || null,
    fwmark: mark ? Number(mark) : null,
    playlist_base_url: fields.get("playlist_base_url").trim() || null,
    auth,
  };
  const submit = settings.querySelector("button[type=submit]");
  submit.disabled = true;
  try {
    await api("provider?name=" + name, update);
    if (provider !== name) return;
    settingsChanged = false;
    const password = settings.elements["auth-password"];
    password.value = "";
    if (auth.password) password.placeholder = "已设置，留空保留原密码";
    message("settings-status", "连接设置已保存。");
  } catch (error) {
    if (provider === name) message("settings-status", error.message, true);
  } finally {
    submit.disabled = false;
  }
});
settings.addEventListener("input", () => { settingsChanged = true; });

function visibleChannels() {
  const query = byId("channel-filter").value.trim().toLowerCase();
  return channels.filter(({ id, name, group }) => (id + " " + name + " " + group).toLowerCase().includes(query));
}

function markChannel(channel) {
  changedChannels.set(channel.id, { enabled: channel.enabled, catchup: channel.catchup });
  message("channel-status", "频道选择有未保存的改动。");
  byId("save-channels").disabled = false;
  updateCount();
}

function updateCount() {
  const selected = channels.filter((channel) => channel.enabled).length;
  byId("channel-count").textContent = "已选 " + selected + " / " + channels.length + " 个频道";
}

function checkbox(checked, label, change) {
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = checked;
  input.setAttribute("aria-label", label);
  input.onchange = () => change(input.checked);
  return input;
}

function renderChannels() {
  byId("channel-empty").hidden = channels.length > 0;
  byId("channel-list").hidden = channels.length === 0;
  byId("save-channels").disabled = changedChannels.size === 0;
  const rows = document.createDocumentFragment();
  const visible = visibleChannels();
  for (const channel of visible) {
    const row = document.createElement("tr");
    const cells = Array.from({ length: 4 }, () => row.insertCell());
    const id = document.createElement("span");
    id.className = "channel-id";
    id.textContent = channel.id;
    const replay = checkbox(channel.has_catchup && channel.catchup, channel.name + " 回看", (checked) => {
      channel.catchup = checked;
      markChannel(channel);
    });
    replay.disabled = !channel.enabled;
    cells[0].append(checkbox(channel.enabled, channel.name + " 播放", (checked) => {
      channel.enabled = checked;
      replay.disabled = !checked;
      markChannel(channel);
    }));
    cells[1].append(channel.name, id);
    cells[2].textContent = channel.group;
    cells[3].append(channel.has_catchup ? replay : "—");
    rows.append(row);
  }
  if (!visible.length) {
    const row = document.createElement("tr");
    const cell = row.insertCell();
    cell.colSpan = 4;
    cell.textContent = "没有匹配的频道";
    rows.append(row);
  }
  byId("channel-rows").replaceChildren(rows);
  updateCount();
}

byId("channel-filter").oninput = renderChannels;

for (const [id, enabled] of [["select-all", true], ["select-none", false]]) {
  byId(id).onclick = () => {
    for (const channel of visibleChannels()) {
      if (channel.enabled === enabled) continue;
      channel.enabled = enabled;
      markChannel(channel);
    }
    renderChannels();
  };
}

byId("save-channels").onclick = async () => {
  const name = provider;
  const pending = new Map(changedChannels);
  byId("save-channels").disabled = true;
  try {
    await api("channels?name=" + name, Object.fromEntries(pending));
    if (provider !== name) return;
    for (const [id, value] of pending) {
      if (changedChannels.get(id) === value) changedChannels.delete(id);
    }
    if (!changedChannels.size) message("channel-status", "已保存。生成 M3U 后生效。");
  } catch (error) {
    if (provider === name) message("channel-status", error.message, true);
  } finally {
    if (provider === name) byId("save-channels").disabled = !changedChannels.size;
  }
};

for (const button of document.querySelectorAll("[data-action]")) {
  button.onclick = async () => {
    const name = provider;
    const action = button.dataset.action;
    message("notice");
    if (action === "playlist" && changedChannels.size) {
      message("notice", "请先保存频道选择。", true);
      return;
    }
    if (settingsChanged) {
      message("notice", "请先保存连接设置。", true);
      return;
    }
    button.disabled = true;
    try {
      await api("jobs?name=" + name, { action });
      jobs[name] = { action, state: "running" };
      updateJob();
      await pollJobs();
    } catch (error) {
      if (provider === name) {
        updateJob();
        message("notice", error.message, true);
      }
    } finally {
      if (provider !== name) updateJob();
    }
  };
}

async function pollJobs() {
  if (!csrf || polling) return;
  polling = true;
  const session = csrf;
  try {
    const previous = jobs;
    const current = await api("jobs");
    if (session !== csrf) return;
    jobs = current;
    const job = jobs[provider];
    updateJob();
    if (job?.state === "done" && previous[provider]?.state !== "done") {
      await loadProvider(provider, true);
    }
  } catch (error) {
    if (session === csrf) message("job-status", error.message, true);
  } finally {
    polling = false;
  }
}

enterConsole().catch(() => {});
setInterval(pollJobs, 2000);
