"use strict";

const $ = (id) => document.getElementById(id);
const CHECK = { disk_encryption: "disk encryption", screen_lock: "screen lock", os_up_to_date: "OS updates" };
const COMPONENT = { ram: "RAM", storage: "storage", battery: "battery" };
const STATUS_LABEL = { answered: "Answered", refused: "Can't help with that", insufficient_data: "Not enough data" };
const SUGGESTIONS = [
  "Which devices are low on disk space?",
  "Show me laptops failing high-severity compliance checks.",
  "How many devices are running an OS older than macOS 15?",
  "Which batteries are approaching end of life?",
  "Open remediation tickets for every device failing the screen lock check.",
];
const FIELD_LABEL = {
  disk_free_pct: "Disk free", disk_free_gb: "Disk free", disk_size_gb: "Disk size", mem_used_pct: "Memory used",
  ram_gb: "RAM", employee_id: "Owner", hostname: "Device", form_factor: "Type", failing_checks: "Failing checks",
  battery_condition: "Battery health", condition: "Battery health", cycle_count: "Charge cycles",
  full_charge_capacity: "Full-charge capacity", capacity_drop_pct: "Capacity drop", days_to_full: "Days until full",
  slope_gb_per_day: "Change per day", share_over_threshold: "Readings over 80%", avg_used_pct: "Average memory used",
  peak_used_pct: "Peak memory used", failing_since: "Failing since", last_pass: "Last passed", first_seen: "First seen",
  consecutive_failures: "Days failing", match_count: "Matches", devices_scanned: "Devices checked",
  model: "Model", os: "OS", processor: "Processor", disk_encrypted: "Disk encrypted", battery: "Battery",
  software: "Software", checks: "Checks", installed: "Installed", version: "Version", approved: "Approved",
  first_capacity: "Capacity at start", last_capacity: "Capacity now", first_cycles: "Cycles at start",
  last_cycles: "Cycles now", condition_since: "Health changed on", cycle_limit: "Cycle limit",
};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || `Request failed (${res.status})`), { status: res.status });
  return data;
}

const state = { busy: false, thread: null, evidence: new Map(), activity: null };

function day(iso) {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00Z` : iso);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function plain(text) {
  return String(text ?? "")
    .replace(/\b\d{4}-\d{2}-\d{2}(T[\d:]+Z)?\b/g, (m) => day(m))
    .replace(/\b(disk_encryption|screen_lock|os_up_to_date)\b/g, (m) => CHECK[m]);
}

function ago(seconds) {
  const diff = Date.now() / 1000 - seconds;
  if (diff < 90) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  return new Date(seconds * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function deviceLine(d) {
  if (!d || !d.hostname) return d?.employee ? `Employee ${d.employee}` : d?.id || "";
  return [d.hostname, d.model, d.owner].filter(Boolean).join(" · ");
}

async function showLogin() {
  for (const id of ["app", "insights", "nav", "who", "signout"]) $(id).hidden = true;
  $("login").hidden = false;
  const tenants = await api("/api/tenants");
  $("tenant").replaceChildren(...tenants.map((t) => el("option", { value: t.id }, `${t.name} (${t.id})`)));
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").hidden = true;
  try {
    await api("/api/login", { method: "POST", body: { tenant: $("tenant").value, actor: $("actor").value.trim() } });
    await start();
  } catch (err) {
    $("login-error").textContent = err.message;
    $("login-error").hidden = false;
  }
});

$("signout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  state.thread = null;
  $("turns").replaceChildren();
  showLogin();
});

async function start() {
  let session;
  try {
    session = await api("/api/session");
  } catch (err) {
    if (err.status === 401) return showLogin();
    throw err;
  }
  $("login").hidden = true;
  for (const id of ["nav", "who", "signout"]) $(id).hidden = false;
  $("who").textContent = `${session.tenant_name} · ${session.actor}`;
  $("suggestions").replaceChildren(...SUGGESTIONS.map((q) => el("button", { type: "button", onclick: () => ask(q) }, q)));
  showView("chat");
  const list = await loadConversations();
  if (list.length) await openConversation(list[0].thread);
  else newConversation();
  refreshBadge();
}

function showView(name) {
  $("app").hidden = name !== "chat";
  $("insights").hidden = name !== "insights";
  document.querySelectorAll("#nav button").forEach((b) => {
    if (b.dataset.view === name) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  if (name === "insights") loadInsights();
}
document.querySelectorAll("#nav button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

async function loadConversations() {
  const list = await api("/api/conversations");
  $("conversations").replaceChildren(...list.map((c) => el("li", {},
    el("button", { type: "button", "aria-current": String(c.thread === state.thread), onclick: () => openConversation(c.thread) },
      el("span", { class: "title" }, c.title), el("span", { class: "when" }, ago(c.updated))))));
  return list;
}

function newConversation() {
  state.thread = null;
  state.evidence.clear();
  $("turns").replaceChildren();
  $("empty").hidden = false;
  $("tab-evidence").replaceChildren(el("p", { class: "muted" }, "Select a citation number to see the telemetry behind it."));
  loadConversations();
  $("question").focus();
}
$("new-chat").addEventListener("click", newConversation);

async function openConversation(thread) {
  if (state.busy) return;
  const conv = await api(`/api/conversations/${thread}`);
  state.thread = thread;
  state.evidence.clear();
  $("turns").replaceChildren();
  $("empty").hidden = conv.turns.length > 0;
  for (const t of conv.turns) renderAnswer(newTurn(t.question), t);
  const last = conv.turns.at(-1);
  renderEvidence(last?.evidence || []);
  loadConversations();
}

function newTurn(question) {
  $("empty").hidden = true;
  const body = el("div", { class: "answer", "aria-live": "polite" });
  const turn = el("article", { class: "turn" }, el("p", { class: "q" }, el("span", { class: "label" }, "You asked"), question), body);
  $("turns").append(turn);
  turn.scrollIntoView({ block: "start" });
  return { root: turn, body };
}

function setBusy(busy) {
  state.busy = busy;
  $("ask-btn").disabled = busy;
  $("ask-btn").textContent = busy ? "Working" : "Ask";
}

async function ask(question) {
  if (state.busy || !question.trim()) return;
  showView("chat");
  setBusy(true);
  $("question").value = "";
  const turn = newTurn(question);
  const steps = el("ul", { class: "steps" }, el("li", {}, "Checking the request"));
  turn.body.replaceChildren(steps, el("div", { class: "skeleton", "aria-hidden": "true" }, el("span"), el("span"), el("span")));
  state.activity = { plan: null, tools: [], verify: [] };
  renderActivity();
  try {
    if (!state.thread) state.thread = (await api("/api/conversations", { method: "POST" })).thread;
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread: state.thread, question }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || `Request failed (${res.status})`);
    }
    await readEvents(res, (kind, data) => {
      if (kind === "plan") {
        state.activity.plan = data;
        steps.append(el("li", {}, `Planned ${data.steps.length} steps`));
      } else if (kind === "tool") {
        state.activity.tools.push(data);
        const ok = ["ok", "queued_for_approval", "already_proposed"].includes(data.status);
        steps.append(el("li", { class: ok ? "ok" : "bad" }, `${data.name.replaceAll("_", " ")}: ${data.status.replaceAll("_", " ")}`));
      } else if (kind === "verify") {
        state.activity.verify.push(data.status);
        steps.append(el("li", {}, data.status === "retry" ? "Evidence check failed, asking the model to fix it"
          : data.status === "verified" ? "Evidence check passed" : "Evidence check removed unsupported statements"));
      } else if (kind === "answer") {
        renderAnswer(turn, data);
      } else if (kind === "error") {
        turn.body.replaceChildren(el("p", { class: "error" }, data.message));
      }
      renderActivity();
    });
  } catch (err) {
    turn.body.replaceChildren(el("p", { class: "error", role: "alert" }, err.message));
  } finally {
    setBusy(false);
    loadConversations();
  }
}

async function readEvents(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const kind = /^event: (.*)$/m.exec(block)?.[1];
      const data = /^data: (.*)$/m.exec(block)?.[1];
      if (kind && data) onEvent(kind, JSON.parse(data));
    }
  }
}

$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  ask($("question").value);
});
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    ask($("question").value);
  }
});

function renderAnswer(turn, a) {
  if (!a.status) {
    turn.body.replaceChildren(el("p", { class: "muted" }, "No answer was produced."));
    return;
  }
  for (const e of a.evidence || []) state.evidence.set(e.id, e);
  const status = el("div", { class: "status" },
    el("span", { class: "tag" }, STATUS_LABEL[a.status] || a.status),
    a.verified === false ? el("span", { class: "tag warn" }, "Partly unverified") : null);
  const findings = a.findings?.length
    ? el("ol", { class: "findings" }, a.findings.map((f) => el("li", {},
        plain(f.statement), f.evidence_ids.map((id, i) => chip(f.refs[i], id)),
        f.devices.map((d) => el("span", { class: "device-line" }, deviceLine(d))))))
    : null;
  const caveats = a.caveats?.length ? el("ul", { class: "caveats" }, a.caveats.map((c) => el("li", {}, plain(c)))) : null;
  const u = a.usage || {};
  const details = el("details", { class: "more" }, el("summary", {}, "Details"),
    el("p", { class: "meta" }, a.verified === false
      ? "Some statements failed the evidence check and were removed."
      : "Every statement was checked against the cited telemetry."),
    el("p", { class: "meta" }, `Data as of ${day(a.data_as_of) || "unknown"}.`),
    u.model_calls != null ? el("p", { class: "meta" },
      `${u.model_calls} model calls, ${(u.input_tokens ?? 0).toLocaleString()} input and ${(u.output_tokens ?? 0).toLocaleString()} output tokens${u.cost_usd != null ? `, about $${u.cost_usd}` : ""}.`) : null);
  turn.body.replaceChildren(...[status, el("p", { class: "summary" }, plain(a.summary)), findings, caveats,
    a.proposals?.length ? renderProposals(turn, a) : null, details].filter(Boolean));
  renderEvidence(a.evidence || []);
}

function chip(number, id) {
  return el("button", { type: "button", class: "chip", title: "Show evidence", "aria-label": `Evidence ${number}`, onclick: () => selectEvidence(id) }, number);
}

function proposalTitle(p) {
  const where = p.target?.hostname || p.target?.employee || p.args.device_id || p.args.employee_id;
  if (p.tool === "open_remediation_ticket") return `Open a ticket: fix ${CHECK[p.args.check_id] || p.args.check_id} on ${where}`;
  if (p.tool === "notify_employee") return `Send a message to ${where}`;
  if (p.tool === "create_upgrade_order") return `Order a ${COMPONENT[p.args.component] || p.args.component} upgrade for ${where}`;
  if (p.tool === "flag_device_for_replacement") return `Flag ${where} for replacement`;
  return `${p.tool} ${where}`;
}

function proposalText(p) {
  const a = p.args;
  if (a.message) return ["Message", a.message];
  if (a.note) return ["Ticket note", a.note];
  if (a.spec) return ["Order", a.spec];
  if (a.reason) return ["Reason", a.reason];
  return null;
}

function renderProposals(turn, a) {
  const choices = new Map();
  const notes = new Map();
  const refNumber = new Map((a.evidence || []).map((e, i) => [e.id, i + 1]));
  const pending = a.pending_approval;
  const box = el("section", { class: "actions", "aria-label": "Proposed actions" },
    el("h3", {}, pending ? "Your decision is needed. Nothing has run yet." : "Proposed actions"));
  const submit = el("button", { type: "button", class: "primary", disabled: true }, "Submit decisions");
  const count = el("span", { class: "count" });

  const refresh = () => {
    const decided = a.proposals.filter((p) => choices.has(p.id)).length;
    const missingNote = a.proposals.some((p) => p.risk === "high" && choices.get(p.id) === "approve" && !notes.get(p.id));
    count.textContent = missingNote ? "High-risk approvals need a note." : `${decided} of ${a.proposals.length} decided`;
    submit.disabled = decided < a.proposals.length || missingNote;
  };

  const cards = [];
  for (const p of a.proposals) {
    const text = proposalText(p);
    const card = el("article", { class: "proposal" },
      el("header", {}, el("strong", {}, proposalTitle(p)), el("span", { class: `risk ${p.risk}` }, `${p.risk} risk`)),
      p.target?.hostname ? el("p", { class: "muted" }, deviceLine(p.target)) : null,
      text ? el("p", {}, el("span", { class: "muted" }, `${text[0]}: `), text[1]) : null,
      el("p", {}, el("span", { class: "muted" }, "Why: "), plain(p.justification),
        p.evidence_ids.map((id) => chip(refNumber.get(id) ?? "ev", id))),
      el("details", { class: "more" }, el("summary", {}, "Details"),
        el("dl", { class: "args" }, el("dt", {}, "Proposal"), el("dd", {}, p.id), el("dt", {}, "Action"), el("dd", {}, p.tool),
          Object.entries(p.args).map(([k, v]) => [el("dt", {}, k.replaceAll("_", " ")), el("dd", {}, String(v))]))));
    if (pending) {
      const note = el("textarea", { rows: 2, maxlength: 500, oninput: (e) => { notes.set(p.id, e.target.value.trim()); refresh(); } });
      const noteBox = el("div", { class: "note", hidden: true },
        el("label", {}, p.risk === "high" ? "Approver note (required for high risk)" : "Note (optional)", note));
      const approveBtn = el("button", { type: "button", class: "approve", "aria-pressed": "false" }, "Approve");
      const rejectBtn = el("button", { type: "button", class: "reject", "aria-pressed": "false" }, "Reject");
      const pick = (value) => {
        choices.set(p.id, value);
        approveBtn.setAttribute("aria-pressed", String(value === "approve"));
        rejectBtn.setAttribute("aria-pressed", String(value === "reject"));
        noteBox.hidden = value !== "approve";
        refresh();
      };
      approveBtn.addEventListener("click", () => pick("approve"));
      rejectBtn.addEventListener("click", () => pick("reject"));
      card.append(el("div", { class: "choice", role: "group", "aria-label": `Decision for ${p.id}` }, approveBtn, rejectBtn), noteBox);
      cards.push({ p, pick });
    } else if (p.outcome) {
      const o = p.outcome;
      const done = o.status === "executed" ? `Done: ${o.reference}` : o.status === "duplicate" ? `Already done: ${o.reference}`
        : o.status === "blocked" ? `Blocked on re-check: ${plain(o.reason)}` : "Rejected";
      card.append(el("p", { class: `outcome ${o.status}` }, done));
    } else {
      card.append(el("p", { class: "muted" }, "Awaiting approval"));
    }
    box.append(card);
  }

  if (pending) {
    const lows = cards.filter((c) => c.p.risk === "low");
    const approveLow = lows.length
      ? el("button", { type: "button", class: "secondary", onclick: () => lows.forEach((c) => c.pick("approve")) },
          `Approve all low-risk (${lows.length})`)
      : null;
    submit.addEventListener("click", async () => {
      submit.disabled = true;
      submit.textContent = "Submitting";
      const decisions = Object.fromEntries(a.proposals.map((p) => [p.id, { decision: choices.get(p.id), note: notes.get(p.id) || "" }]));
      try {
        const result = await api("/api/approve", { method: "POST", body: { thread: state.thread, token: a.approval_token, decisions } });
        renderAnswer(turn, result);
        const status = (s) => result.proposals.filter((p) => p.outcome?.status === s).length;
        const earlier = status("duplicate");
        turn.body.prepend(el("p", { class: "outcome executed", role: "status" },
          `${status("executed") + earlier} of ${result.proposals.length} actions done`
          + (earlier ? ` (${earlier} had already been done earlier, so nothing ran twice)` : "")
          + ". Every decision is in the audit log."));
      } catch (err) {
        count.textContent = err.message;
        submit.disabled = false;
        submit.textContent = "Submit decisions";
      }
    });
    box.append(el("div", { class: "decide" }, count, approveLow, submit));
    refresh();
  }
  return box;
}

function fieldValue(key, v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.map((x) => fieldValue("", x)).join(", ");
  if (typeof v === "object") return Object.entries(v).map(([k, x]) => `${CHECK[k] || k.replaceAll("_", " ")}: ${fieldValue(k, x)}`).join("; ");
  if (typeof v === "number") {
    if (key.endsWith("_pct")) return `${Math.round(v * 10) / 10}%`;
    if (key.endsWith("_gb") || key === "slope_gb_per_day") return `${Math.round(v * 10) / 10} GB`;
    if (key.startsWith("share")) return `${Math.round(v * 100)}%`;
  }
  return plain(v);
}

function evidenceCard(e, number) {
  const when = e.collected_at ? `Reading of ${day(e.collected_at)}`
    : e.window ? `${day(e.window.from)} to ${day(e.window.to)} (${e.window.snapshots} daily readings)` : "";
  const fields = Object.entries(e.fields || {}).filter(([k]) => !["query", "device_ids", "hostname"].includes(k));
  return el("article", { class: "evidence", "data-id": e.id },
    el("div", { class: "head" }, number ? el("span", { class: "num" }, `[${number}]`) : null,
      el("strong", {}, e.fields?.hostname || (e.fields?.query ? "Fleet search" : "Evidence"))),
    when ? el("div", { class: "muted" }, when) : null,
    fields.length ? el("dl", {}, fields.map(([k, v]) =>
      [el("dt", {}, FIELD_LABEL[k] || k.replaceAll("_", " ")), el("dd", {}, fieldValue(k, v))])) : null,
    el("details", { class: "more" }, el("summary", {}, "Show raw record"),
      el("pre", { class: "mono" }, JSON.stringify(e, null, 1))));
}

function renderEvidence(list) {
  const panel = $("tab-evidence");
  if (!list.length) {
    panel.replaceChildren(el("p", { class: "muted" }, "This answer cites no evidence."));
    return;
  }
  panel.replaceChildren(...list.map((e, i) => evidenceCard(e, i + 1)));
}

async function selectEvidence(id) {
  showTab("evidence");
  const panel = $("tab-evidence");
  let card = [...panel.querySelectorAll(".evidence")].find((c) => c.dataset.id === id);
  if (!card) {
    const record = state.evidence.get(id)
      || await api(`/api/evidence?thread=${encodeURIComponent(state.thread)}&id=${encodeURIComponent(id)}`).catch(() => null);
    if (!record) return;
    state.evidence.set(id, record);
    card = evidenceCard(record);
    panel.prepend(card);
  }
  panel.querySelectorAll(".selected").forEach((c) => c.classList.remove("selected"));
  card.classList.add("selected");
  card.scrollIntoView({ block: "nearest" });
}

function renderActivity() {
  const panel = $("tab-activity");
  const a = state.activity;
  const parts = [];
  if (a?.plan) parts.push(el("h3", {}, `Plan: ${a.plan.intent}`), el("ol", { class: "findings" }, a.plan.steps.map((s) => el("li", {}, s))));
  if (a?.tools.length) {
    parts.push(el("h3", {}, "Tool calls"), el("ul", { class: "steps" }, a.tools.map((t) =>
      el("li", { class: t.status === "ok" ? "ok" : "" }, `${t.name.replaceAll("_", " ")}: ${t.status.replaceAll("_", " ")}`))));
  }
  if (a?.verify.length) parts.push(el("h3", {}, "Evidence check"), el("p", {}, a.verify.join(", then ")));
  panel.replaceChildren(...(parts.length ? parts : [el("p", { class: "muted" }, "The plan and every tool call of the last question.")]));
}

function auditDetail(e) {
  const skip = new Set(["seq", "ts", "session", "tenant", "actor", "event"]);
  return Object.entries(e).filter(([k, v]) => !skip.has(k) && v !== null && v !== "")
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  ").slice(0, 400);
}

async function renderAudit() {
  const panel = $("tab-audit");
  if (!state.thread) return panel.replaceChildren(el("p", { class: "muted" }, "No events yet in this conversation."));
  panel.replaceChildren(el("div", { class: "skeleton", "aria-hidden": "true" }, el("span"), el("span"), el("span")));
  try {
    const rows = await api(`/api/audit?thread=${encodeURIComponent(state.thread)}`);
    const refresh = el("button", { type: "button", class: "secondary", onclick: renderAudit }, "Refresh");
    if (!rows.length) return panel.replaceChildren(refresh, el("p", { class: "muted" }, "No events yet in this conversation."));
    panel.replaceChildren(refresh, el("table", { class: "audit" },
      el("thead", {}, el("tr", {}, el("th", {}, "#"), el("th", {}, "Time"), el("th", {}, "Event"), el("th", {}, "Detail"))),
      el("tbody", {}, rows.slice().reverse().map((e) => el("tr", {},
        el("td", {}, e.seq), el("td", { class: "mono" }, e.ts.slice(11, 19)), el("td", {}, e.event.replaceAll("_", " ")),
        el("td", { class: "mono" }, auditDetail(e)))))));
  } catch (err) {
    panel.replaceChildren(el("p", { class: "error" }, err.message));
  }
}

function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  for (const t of ["evidence", "activity", "audit"]) $(`tab-${t}`).hidden = t !== name;
  if (name === "audit") renderAudit();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

async function refreshBadge() {
  try {
    const b = await api("/api/insights");
    $("urgent-badge").textContent = b.new_urgent;
    $("urgent-badge").hidden = !b.new_urgent;
    $("urgent-badge").setAttribute("aria-label", `${b.new_urgent} new urgent issues`);
  } catch {
    $("urgent-badge").hidden = true;
  }
}

const GROUPS = [
  ["Needs attention", ["critical", "high"]],
  ["Plan for", ["medium"]],
  ["Data quality", ["low"]],
];

async function loadInsights(date) {
  const body = $("insights-body");
  body.replaceChildren(el("div", { class: "skeleton", "aria-hidden": "true" }, el("span"), el("span"), el("span")));
  try {
    const days = await api("/api/insights/days");
    if (!days.length) {
      $("insights-sub").textContent = "The first briefing is being prepared. Check back in a minute.";
      body.replaceChildren();
      return;
    }
    const picker = $("insights-day");
    picker.replaceChildren(...days.map((d) => el("option", { value: d.date },
      `${day(d.date)}${d.new_urgent ? ` (${d.new_urgent} new urgent)` : ""}`)));
    picker.value = date || days[0].date;
    picker.onchange = () => loadInsights(picker.value);
    renderBriefing(await api(`/api/insights?date=${picker.value}`));
  } catch (err) {
    body.replaceChildren(el("p", { class: "error" }, err.message));
  }
}

function renderBriefing(b) {
  $("insights-sub").textContent = `Telemetry as of ${day(b.data_as_of)}. Refreshed every day; "New" compares with the day before.`;
  $("insights-counts").replaceChildren(...["critical", "high", "medium", "low"].map((s) =>
    el("div", { class: `count ${s}` }, el("strong", {}, b.counts[s]), s)));
  $("insights-body").replaceChildren(...GROUPS.map(([title, sevs]) => {
    const items = b.issues.filter((i) => sevs.includes(i.severity));
    if (!items.length) return null;
    return el("section", { class: "issue-group" }, el("h2", {}, `${title} (${items.length})`), items.map(issueRow));
  }).filter(Boolean));
}

function issueRow(i) {
  const act = i.suggestion
    ? el("button", { type: "button", class: "secondary act", onclick: () => handOff(i.suggestion.request) }, `Ask Copilot: ${i.suggestion.label}`)
    : el("span", { class: "muted act" }, "No action needed");
  return el("article", { class: "issue" },
    el("div", { class: "head" }, el("span", { class: `sev ${i.severity}` }, i.severity), el("strong", {}, i.title),
      i.new ? el("span", { class: "new" }, "New") : null),
    act,
    el("p", {}, el("span", { class: "muted" }, `${deviceLine(i.device)}. `), i.why));
}

function handOff(request) {
  newConversation();
  ask(request);
}

start().catch((err) => {
  document.body.append(el("p", { class: "error", role: "alert" }, `Could not load: ${err.message}`));
});
