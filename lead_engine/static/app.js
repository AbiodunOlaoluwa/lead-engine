// Lead Engine demo app. Plain JS, no build step. Every button calls the same pipeline as the CLI.
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const view = $("#view");

const STAGES = [
  { key: "source", name: "Source", note: "Polite crawl, robots.txt, retries" },
  { key: "extract", name: "Extract", note: "Free-text pages, grounded in quotes" },
  { key: "enrich", name: "Enrich", note: "Providers tried in order" },
  { key: "verify", name: "Verify", note: "Catch-all domains flagged" },
  { key: "match", name: "Match", note: "Same contact, across sources" },
  { key: "score", name: "Score", note: "Points anyone can read" },
];

const SOURCE_NAMES = { portal: "Property portal", register: "Professional register", profile: "Firm profile pages" };

let state = null;
let pace = "watch";
let contactFilter = "all";
let contactQuery = "";
let selectedContact = null;

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(body?.detail ?? `Request failed (${res.status})`);
  return body;
}
const post = (path, data = {}) => api(path, { method: "POST", body: JSON.stringify(data) });

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 3200);
}

async function refresh() {
  state = await api("/api/state");
  const mode = $("#mode");
  mode.textContent = state.mode === "claude" ? "Claude extraction" : "Regex extraction";
  mode.classList.toggle("claude", state.mode === "claude");
  const rc = $("#review-count");
  rc.hidden = !state.summary.review_pairs;
  rc.textContent = state.summary.review_pairs;
  return state;
}

// The server pushes the job's real state the moment the engine changes it (server-sent events).
// The UI only ever draws what it was told: it never guesses progress and never finishes early.
let stream = null;
function connectStream() {
  if (stream) return;
  stream = new EventSource("/api/job/stream");
  stream.onmessage = (e) => onJob(JSON.parse(e.data));
  stream.onerror = () => {
    stream.close();
    stream = null;
    setTimeout(connectStream, 1000); // the server restarted; reconnect and resync
  };
}

async function onJob(job) {
  if (!state) return;
  const was = state.job?.state;
  state.job = job;
  if ((job.kind === "run" || job.kind === "rescore") && job.state === "done" && job.result) state.summary = job.result; // final numbers arrive with the "done" message
  const tab = currentTab();
  if (tab === "run") renderRunLive();
  if (tab === "evals" && job.kind === "eval") renderEvalsLive();
  if (was === "running" && job.state !== "running") {
    await refresh(); // review counts, contacts, has_run
    if (tab !== "run" || job.kind === "generate") route();
    else renderRunLive();
  }
}

async function startJob(path, data, goTo = "run") {
  try {
    await post(path, data);
    if (goTo && currentTab() !== goTo) location.hash = `#${goTo}`;
  } catch (e) {
    toast(e.message);
  }
}

// ---------------------------------------------------------------- Run
function tile(value, label, cls = "") {
  return `<div class="tile ${cls}"><b>${esc(value)}</b><span>${esc(label)}</span></div>`;
}

function summaryTiles(s) {
  return [
    tile(s.pages_fetched, "pages fetched"),
    tile(s.pages_disallowed, "page skipped by robots.txt", s.pages_disallowed ? "held" : ""),
    tile(s.listings, "listings from 3 sources"),
    tile(s.parse_gaps, "pages where we kept fewer than we saw", s.parse_gaps ? "bad" : "ok"),
    tile(s.deliverable, "deliverable emails", "ok"),
    tile(s.catch_all_or_quarantined, "catch-all addresses flagged", "bad"),
    tile(s.contacts, "contacts after matching"),
    tile(s.review_pairs, "pairs waiting for you", s.review_pairs ? "held" : ""),
    tile(s.tier1, "tier 1 contacts", "ok"),
    tile(s.outside_icp, "outside the ICP, held back from export"),
  ].join("");
}

function renderRun() {
  const job = state.job;
  const running = job.state === "running";
  const resumable = state.has_run && (job.state === "interrupted" || (job.kind === "run" && job.state === "interrupted"));
  view.innerHTML = `
  <section class="run-top">
    <div class="run-intro">
      <p class="label">Lead Engine · fictional data</p>
      <h1>Three messy directories in. One clean, scored contact list out.</h1>
      <p class="lede">A property portal with two page layouts, a professional register that writes names surname first, and free-text profile pages. Every button here runs the same pipeline as the command line.</p>
      <div class="facts">
        <span class="fact">${state.site.firms} firms</span><span class="fact">${state.site.listings} listings</span>
        <span class="fact">.example domains</span><span class="fact">0800 555 numbers</span>
        <span id="icp-facts" style="display:contents"></span>
      </div>
    </div>
    <div class="card controls">
      <div class="control-row">
        <button class="btn btn-accent btn-lg" id="run-btn" ${running ? "disabled" : ""}>${state.has_run ? "Run again" : "Run the pipeline"}</button>
        <div class="segmented" role="group" aria-label="Pace">
          <button type="button" data-pace="watch" class="${pace === "watch" ? "on" : ""}">Watchable</button>
          <button type="button" data-pace="fast" class="${pace === "fast" ? "on" : ""}">Fast</button>
        </div>
        <p class="control-help">"Run again" re-uses everything already done, so it finishes almost instantly. That's the ledger at work. "Start over" clears it.</p>
      </div>
      <div class="control-row">
        <label for="crash-at" class="muted">Simulate a crash after</label>
        <input id="crash-at" class="inline-num" type="number" min="1" max="2000" value="60">
        <span class="muted">steps</span>
        <button class="btn btn-red btn-sm" id="crash-btn" ${running ? "disabled" : ""}>Run until it crashes</button>
        <button class="btn btn-amber btn-sm" id="resume-btn" ${running || !resumable ? "disabled" : ""}>Resume</button>
        <p class="control-help">Starts fresh, stops part-way like a crash, then Resume picks up exactly where it stopped. Nothing is fetched or looked up twice.</p>
      </div>
      <div class="control-row">
        <button class="btn btn-ghost btn-sm" id="drill-btn" ${running ? "disabled" : ""}>Run the crash drill</button>
        <button class="btn btn-ghost btn-sm" id="reset-btn" ${running ? "disabled" : ""}>Start over</button>
        <p class="control-help">The drill crashes a run three times on a scratch copy, resumes it, and compares the result with an uninterrupted run.</p>
      </div>
    </div>
  </section>
  <section class="rail" id="rail" aria-label="Pipeline stages"></section>
  <div id="status"></div>
  <section class="tiles" id="tiles"></section>
  <section class="two-col">
    <div class="card" id="drill-card"></div>
    <div class="card"><div class="section-head"><h2 style="font-size:19px">What just happened</h2><span class="mono muted">latest first</span></div><ul class="events" id="events"></ul></div>
  </section>`;

  $("#run-btn").onclick = () => startJob("/api/jobs/run", { pace });
  $("#crash-btn").onclick = () => startJob("/api/jobs/run", { pace, fresh: true, stop_after: Number($("#crash-at").value) || 60 });
  $("#resume-btn").onclick = () => startJob("/api/jobs/run", { pace });
  $("#drill-btn").onclick = () => startJob("/api/jobs/drill", {});
  $("#resume-btn").disabled = !(state.job.state === "interrupted" && state.job.kind === "run");
  $("#reset-btn").onclick = async () => {
    try {
      await post("/api/reset");
      toast("Cleared. The directory is still there; run the pipeline to process it again.");
      await refresh();
      route();
    } catch (e) {
      toast(e.message);
    }
  };
  view.querySelectorAll("[data-pace]").forEach((b) =>
    b.addEventListener("click", () => {
      pace = b.dataset.pace;
      view.querySelectorAll("[data-pace]").forEach((x) => x.classList.toggle("on", x === b));
    }),
  );
  renderRunLive();
}

function renderRunLive() {
  const job = state.job;
  const rail = $("#rail");
  if (!rail) return;
  const fromLastRun = job.kind === "none" && state.has_run;
  rail.innerHTML = STAGES.map((s, i) => {
    const st = job.stages[s.key];
    let cls = st.state; // pending | running | done | stopped, exactly as the engine reported it
    if (fromLastRun) cls = "done";
    if (job.kind === "eval" || job.kind === "generate") cls = fromLastRun ? "done" : state.has_run ? "done" : "pending";
    const pct = st.total ? Math.min(100, Math.round((st.done / st.total) * 100)) : cls === "done" || cls === "kept" ? 100 : 0;
    const count =
      fromLastRun || job.kind === "eval" || job.kind === "generate"
        ? state.has_run ? "done on the last run" : "waiting"
        : cls === "kept" ? "kept from the last run"
        : cls === "stopped" ? `crashed at ${st.done} of ${st.total}`
        : cls === "done" ? (st.total ? `${st.total} of ${st.total}` : "nothing new to do")
        : cls === "running" ? (st.total ? `${st.done} of ${st.total}` : "planning…")
        : "waiting";
    const issues = [
      st.warn ? `<span class="chip c-held">${st.warn} warning${st.warn === 1 ? "" : "s"}</span>` : "",
      st.error ? `<span class="chip c-bad">${st.error} error${st.error === 1 ? "" : "s"}</span>` : "",
    ].join(" ");
    return `<article class="station ${cls}">
      <span class="idx">${String(i + 1).padStart(2, "0")} ${cls === "running" ? '<span class="pulse" aria-hidden="true"></span>' : ""}</span>
      <h3>${s.name}</h3><p>${s.note}</p>
      <div class="bar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" aria-label="${s.name}"><span style="width:${pct}%"></span></div>
      <span class="count-line">${count}</span>
      ${issues ? `<div class="issues">${issues}</div>` : ""}
      ${st.last && !fromLastRun ? `<span class="last" title="${esc(st.last)}">${esc(st.last)}</span>` : ""}
    </article>`;
  }).join("");

  const label = { run: "the pipeline", drill: "the crash drill", eval: "the evals", generate: "the generator", rescore: "the rescore" }[job.kind] ?? "";
  const blocked = state.blocked && job.state !== "running";
  const text =
    job.state === "running"
      ? `Running ${label}${job.current ? `: ${job.current}` : ""} · ${job.seconds}s`
      : blocked ? state.blocked
      : job.message || (state.has_run ? "Results from the last run are below. Run again, crash it, or explore the other tabs." : "Nothing has run yet. Press Run the pipeline.");
  $("#status").innerHTML = `<div class="status ${blocked ? "failed" : job.state}">${job.state === "running" ? '<span class="pulse"></span>' : ""}<span>${esc(text)}${blocked ? ' <a href="#sources">Open Sources</a>' : ""}${job.phase ? `<span class="phase">${esc(job.phase)}</span>` : ""}</span></div>`;

  // Numbers only show once the engine says the run is finished; mid-run they would be half-true.
  const midRun = (job.state === "running" || job.state === "interrupted") && (job.kind === "run" || job.kind === "drill");
  $("#tiles").innerHTML = !state.has_run
    ? ""
    : midRun
      ? `<p class="partial">${job.state === "interrupted" ? "The run stopped part-way, so its totals would be half-true. Resume it and they appear here." : "Totals appear the moment the engine reports the run finished."}</p>`
      : summaryTiles(state.summary);
  for (const id of ["run-btn", "crash-btn", "drill-btn", "reset-btn"]) {
    const b = $("#" + id);
    if (b) b.disabled = job.state === "running" || (id !== "reset-btn" && !!state.blocked);
  }
  const resume = $("#resume-btn");
  if (resume) resume.disabled = !(job.state === "interrupted" && job.kind === "run");

  const drill = $("#drill-card");
  if (job.kind === "rescore" && job.result?.rescore) {
    const r = job.result.rescore;
    const line = (name, k) => `<tr><td>${name}</td><td class="num">${r.before[k]}</td><td class="num">${r.after[k]}</td></tr>`;
    drill.innerHTML = `<div class="section-head"><h2 style="font-size:19px">Rescore</h2><span class="chip c-ok">ICP version ${r.version}</span></div>
      <p class="lede" style="margin-top:8px">Only the score stage ran. The pages, extractions, lookups and matches from the last run were kept as they were.</p>
      <table style="margin-top:10px"><thead><tr><th>Contacts</th><th>Before</th><th>After</th></tr></thead><tbody>
        ${line("Tier 1", "tier1")}${line("Tier 2", "tier2")}${line("Tier 3", "tier3")}${line("Outside the ICP", "outside")}
      </tbody></table>
      <p style="margin-top:10px"><a class="btn btn-ghost btn-sm" href="#contacts">See the contacts</a> <a class="btn btn-ghost btn-sm" href="#target">Back to Target</a></p>`;
  } else if (job.kind === "drill" && job.state === "done" && job.result) {
    const r = job.result;
    const ok = Object.keys(r.differences).length === 0;
    drill.innerHTML = `<div class="section-head"><h2 style="font-size:19px">Crash drill</h2>${ok ? '<span class="chip c-ok">passed</span>' : '<span class="chip c-bad">failed</span>'}</div>
      <p class="lede" style="margin-top:8px">Crashed after ${r.stopped_at.join(", ")} steps, resumed each time, then compared with a run that never stopped.</p>
      <table style="margin-top:10px"><tbody>
        <tr><td>Listings stored</td><td class="num">${r.checks.listings}</td></tr>
        <tr><td>Distinct listing ids</td><td class="num">${r.checks.distinct_listing_ids}</td></tr>
        <tr><td>Pages fetched twice</td><td class="num">${r.checks.pages_fetched_twice}</td></tr>
        <tr><td>Enrichment lookups repeated</td><td class="num">${r.checks.enrichment_lookups_repeated}</td></tr>
        <tr><td>Differences from a clean run</td><td class="num">${ok ? "none" : esc(JSON.stringify(r.differences))}</td></tr>
      </tbody></table>`;
  } else if (job.kind !== "drill" || job.state !== "running") {
    drill.innerHTML = `<div class="section-head"><h2 style="font-size:19px">Crash drill</h2></div>
      <p class="lede" style="margin-top:8px">Pipelines crash: a laptop sleeps, a provider times out, a deploy restarts the worker. The drill proves this one can stop anywhere and resume without fetching, creating or looking up anything twice.</p>`;
  } else {
    drill.innerHTML = `<div class="section-head"><h2 style="font-size:19px">Crash drill</h2><span class="chip c-held">running</span></div><p class="lede" style="margin-top:8px">${esc(job.phase)}</p>`;
  }
  const facts = $("#icp-facts");
  if (facts)
    facts.innerHTML = `<a class="fact fact-link" href="#target">Target: ICP version ${state.icp.version}</a>${
      state.has_run && state.icp.scored_with && state.icp.scored_with !== state.icp.version && job.state !== "running"
        ? `<a class="fact fact-held" href="#target">Contacts scored with version ${state.icp.scored_with}: rescore</a>` : ""}`;
  renderEvents(job);
}

function renderEvents(job) {
  const ul = $("#events");
  if (!ul) return;
  const row = (e) =>
    `<li><span class="chip ${e.level === "error" ? "c-bad" : e.level === "warn" ? "c-held" : "c-muted"}">${esc(e.level)}</span><span class="mono">${esc(e.stage)}</span><span>${esc(e.message)}</span></li>`;
  if (job.kind !== "none" && job.events.length) {
    ul.innerHTML = job.events.slice().reverse().slice(0, 14).map(row).join("");
    return;
  }
  if (job.kind !== "none") {
    ul.innerHTML = `<li><span></span><span></span><span class="muted">${job.state === "running" ? "Watching for retries, timeouts, catch-alls and crashes…" : "Nothing went wrong on this run."}</span></li>`;
    return;
  }
  api("/api/events?limit=14").then((evs) => {
    const list = evs.filter((e) => e.level !== "info");
    ul.innerHTML = list.length ? list.map(row).join("") : `<li><span></span><span></span><span class="muted">Warnings, retries and skips show up here.</span></li>`;
  });
}

// ---------------------------------------------------------------- Contacts
async function renderContacts() {
  if (!state.has_run) return emptyRun("No contacts yet.");
  const rows = await api(`/api/contacts?q=${encodeURIComponent(contactQuery)}`);
  if (currentTab() !== "contacts") return;
  const filtered = rows.filter((r) =>
    contactFilter === "tier1" ? r.tier === 1 : contactFilter === "tier2" ? r.tier === 2 : contactFilter === "review" ? r.needs_review
    : contactFilter === "noemail" ? !r.email : contactFilter === "outside" ? r.outside_icp : true,
  );
  const held = rows.filter((r) => r.needs_review).length;
  const outside = rows.filter((r) => r.outside_icp && !r.needs_review).length;
  view.innerHTML = `
  <div class="section-head"><h2>Contacts</h2><span class="mono muted">${rows.length} contacts from ${state.summary.listings} listings</span></div>
  <div class="toolbar">
    <div class="toolbar-left">
      <input class="search" id="q" type="search" placeholder="Search name, firm or city" value="${esc(contactQuery)}" aria-label="Search contacts">
      <div class="filters">
        ${[["all", "All"], ["tier1", "Tier 1"], ["tier2", "Tier 2"], ["review", "Waiting for review"], ["outside", "Outside the ICP"], ["noemail", "No email"]]
          .map(([k, l]) => `<button class="filter ${contactFilter === k ? "on" : ""}" data-f="${k}">${l}</button>`)
          .join("")}
      </div>
    </div>
    <a class="btn btn-dark btn-sm" href="/api/export.csv">Export CSV · ${rows.length - held - outside} ready${held ? `, ${held} held for review` : ""}${outside ? `, ${outside} outside the ICP` : ""}</a>
  </div>
  <div class="contacts-grid">
    <div class="table-wrap"><table>
      <thead><tr><th>Contact</th><th>City</th><th>Email</th><th>Sources</th><th>Score</th></tr></thead>
      <tbody>${filtered
        .map(
          (r) => `<tr class="pick ${selectedContact === r.id ? "on" : ""}" data-id="${esc(r.id)}">
          <td><strong>${esc(r.name)}</strong><span class="sub">${esc(r.firm)}</span></td>
          <td>${esc(r.city)}</td>
          <td>${r.email ? `<span class="mono">${esc(r.email)}</span><span class="sub">${statusChip(r.email_status)}</span>` : '<span class="chip c-muted">none found</span>'}</td>
          <td>${r.sources.map((s) => `<span class="chip c-info">${esc(s)}</span>`).join(" ")}${r.needs_review ? ' <span class="chip c-held">review</span>' : ""}${r.outside_icp ? ' <span class="chip c-bad">outside ICP</span>' : ""}</td>
          <td><span class="pts">${r.score}</span><span class="sub">${tierChip(r.tier)}</span></td></tr>`,
        )
        .join("")}</tbody></table>${filtered.length ? "" : '<p class="empty">Nothing matches.</p>'}</div>
    <aside class="detail" id="detail" aria-label="Contact detail"><div class="empty">Pick a contact to see its full trail: how each listing was read, enriched, checked and joined.</div></aside>
  </div>`;
  const q = $("#q");
  q.oninput = debounce(() => {
    contactQuery = q.value;
    renderContacts().then(() => $("#q")?.focus());
  }, 250);
  view.querySelectorAll("[data-f]").forEach((b) => (b.onclick = () => ((contactFilter = b.dataset.f), renderContacts())));
  view.querySelectorAll("tr.pick").forEach((tr) => (tr.onclick = () => showContact(tr.dataset.id)));
  if (selectedContact) showContact(selectedContact, false);
}

function breakdownRow(p) {
  const mark = p.kind === "outside" ? "✕" : p.kind === "must" ? "✓" : `${p.points > 0 ? "+" : ""}${p.points}`;
  const words = (p.evidence || []).map((e) => `“${esc(e)}”`).join(", ");
  return `<tr class="${p.kind === "outside" ? "out" : ""}"><td class="pts ${p.points < 0 || p.kind === "outside" ? "neg" : p.kind === "must" ? "ok" : ""}">${mark}</td>
    <td>${esc(p.why)}${words ? `<span class="sub">ICP: ${words}</span>` : ""}</td></tr>`;
}

function statusChip(s) {
  const map = { deliverable: "c-ok", catch_all: "c-bad", quarantined: "c-bad", invalid: "c-bad", unknown: "c-held" };
  return `<span class="chip ${map[s] ?? "c-muted"}">${esc(s ?? "not checked")}</span>`;
}
function tierChip(t) {
  return `<span class="chip ${t === 1 ? "c-ok" : t === 2 ? "c-held" : "c-muted"}">tier ${t}</span>`;
}

async function showContact(id, scroll = true) {
  selectedContact = id;
  view.querySelectorAll("tr.pick").forEach((tr) => tr.classList.toggle("on", tr.dataset.id === id));
  const d = await api(`/api/contacts/${encodeURIComponent(id)}`);
  const el = $("#detail");
  if (!el) return;
  el.innerHTML = `
    <div class="card"><div class="section-head"><h2 style="font-size:20px">${esc(d.name)}</h2><span>${d.outside_icp ? '<span class="chip c-bad">outside the ICP</span> ' : ""}${tierChip(d.tier)}</span></div>
      <p class="muted" style="margin-top:4px">${d.listings.length} listing${d.listings.length === 1 ? "" : "s"} · ${d.score} points · scored with <a href="#target">ICP version ${d.icp_version ?? "?"}</a></p>
      <table class="breakdown" style="margin-top:10px"><tbody>${d.breakdown.map(breakdownRow).join("")}</tbody></table>
    </div>
    ${d.listings
      .map(
        (l) => `<div class="trail-card">
        <div class="row"><strong>${esc(l.source[0].toUpperCase() + l.source.slice(1))}</strong><span class="chip c-info">${esc(l.layout)}</span><a class="mono muted" href="/site/${esc(l.url)}" target="_blank" rel="noopener">view page</a></div>
        <div>Written as <b>${esc(l.contact_raw || "(no name)")}</b>${l.read_as ? `, read as <b>${esc(l.read_as)}</b>` : ""}${l.source === "register" ? ' <span class="muted">(register: surname first)</span>' : ""}</div>
        <div>Firm: ${esc(l.company)} → <span class="mono">${esc(l.company_norm)}</span></div>
        <div>Phones: <span class="mono">${esc(l.phones.join(", ") || "none")}</span></div>
        <div>Email: <span class="mono">${esc(l.email || "none")}</span> ${statusChip(l.email_status)} ${l.email_source ? `<span class="muted">via ${esc(l.email_source)}</span>` : ""}</div>
        ${l.trail.length ? `<ol>${l.trail.map((t) => `<li><b>${esc(t.provider)}</b>: ${esc(t.result)}${t.email ? " → " + esc(t.email) : ""}</li>`).join("")}</ol>` : ""}
        ${l.source === "profile" ? `<div class="muted">Extracted by ${esc(l.extracted_by)}; ${l.dropped.length} ungrounded field${l.dropped.length === 1 ? "" : "s"} dropped.</div>` : ""}
      </div>`,
      )
      .join("")}
    ${d.links.length ? `<div class="card"><p class="label">Why these listings are one contact</p><ul class="decided">${d.links
      .map((k) => `<li><span class="chip ${k.decision === "linked" ? "c-ok" : "c-held"}">${esc(k.decision)}</span>${esc(k.reasons.join("; "))}</li>`)
      .join("")}</ul></div>` : ""}`;
  if (scroll && window.innerWidth < 1080) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------- Review
async function renderReview() {
  if (!state.has_run) return emptyRun("Nothing to review yet.");
  const q = await api("/api/review");
  if (currentTab() !== "review") return;
  const side = (s) => `<div class="side"><strong>${esc(s.name || "(no name)")}</strong>
    ${s.read_as && s.read_as.toLowerCase() !== (s.name || "").toLowerCase() ? `<span class="muted">read as ${esc(s.read_as)}</span>` : ""}
    <span>${esc(s.company)}</span><span class="muted">${esc(s.city || "")} · <span class="mono">${esc(s.phones.join(", ") || "no phone")}</span></span>
    <span class="mono muted">${esc(s.source)} · ${esc(s.id)}</span></div>`;
  view.innerHTML = `
  <div class="section-head"><h2>Waiting for you</h2><span class="mono muted">${q.pending.length} pairs</span></div>
  <p class="lede">These look like the same person by name, but share no phone number or personal email. A wrong merge loses a real lead and can message someone twice under the wrong name, so the engine never merges on a name alone. You decide.</p>
  <div class="queue">${
    q.pending.length
      ? q.pending
          .map(
            (p, i) => `<article class="pair">
        <div class="pair-sides">${side(p.a)}<span class="vs">score ${p.score.toFixed(2)}</span>${side(p.b)}</div>
        <div class="reasons">${p.reasons.map((r) => `<span class="chip c-info">${esc(r)}</span>`).join("")}</div>
        <div class="pair-actions">
          <button class="btn btn-ghost btn-sm" data-i="${i}" data-d="keep_apart">Different people: keep apart</button>
          <button class="btn btn-accent btn-sm" data-i="${i}" data-d="merge">Same person: merge</button>
        </div></article>`,
          )
          .join("")
      : '<p class="empty">The queue is clear.</p>'
  }</div>
  ${q.decided.length ? `<div class="card"><p class="label">Decided</p><ul class="decided">${q.decided
    .map((d) => `<li><span class="chip ${d.decision === "merge" ? "c-ok" : "c-muted"}">${d.decision === "merge" ? "merged" : "kept apart"}</span>${esc(d.a.name)} · ${esc(d.b.name)} <span class="mono muted">${esc(d.at)}</span></li>`)
    .join("")}</ul></div>` : ""}`;
  view.querySelectorAll("[data-d]").forEach(
    (b) =>
      (b.onclick = async () => {
        const p = q.pending[Number(b.dataset.i)];
        try {
          await post("/api/review", { a: p.a.id, b: p.b.id, decision: b.dataset.d });
          toast(b.dataset.d === "merge" ? "Merged. The contact was re-scored." : "Kept apart.");
          await refresh();
          renderReview();
        } catch (e) {
          toast(e.message);
        }
      }),
  );
}

// ---------------------------------------------------------------- Target (ICP)
// The draft lives here until it is saved, so switching tabs never loses an edit.
let target = null;

async function renderTarget() {
  const meta = await api("/api/icp");
  if (currentTab() !== "target") return; // the user moved on while this loaded
  if (!target || !target.dirty) target = { meta, draft: meta.icp, dirty: false, coverage: meta.coverage, note: "", cost: 0, text: meta.icp.description };
  else {
    target.meta = meta;
    target.coverage = await post("/api/icp/coverage", { icp: target.draft }); // sources may have changed since
  }
  if (currentTab() !== "target") return;
  drawTarget();
}

const TYPE_NAMES = { management: "Property management", letting: "Letting", sales: "Sales" };
const ROLE_NAMES = { principal: "Whoever runs the firm", agent: "The portal agent" };
const FIELD_NAMES = { firm_type: "Kind of firm", rental_book: "Rental book", short_lets: "Short-lets", register: "Register", location: "City", role: "Who you reach", channel: "Reachable by", mostly_sales: "Mostly sales", exclude_locations: "Cities to leave out", "*": "Everything" };

function pills(k, field, values, chosen, names = {}) {
  return `<div class="pills" role="group">${values
    .map((v) => `<button type="button" class="pill ${chosen.includes(v) ? "on" : ""}" data-k="${k}" data-list="${field}" data-v="${esc(v)}" aria-pressed="${chosen.includes(v)}">${esc(names[v] ?? v)}</button>`)
    .join("")}</div>`;
}

function seg(k, field, options, current) {
  return `<div class="segmented" role="group">${options
    .map(([v, l]) => `<button type="button" class="${String(current) === String(v) ? "on" : ""}" data-k="${k}" data-set="${field}" data-v="${v}">${esc(l)}</button>`)
    .join("")}</div>`;
}

function critRow(k) {
  const c = target.draft.criteria[k];
  const label = target.meta.labels[k];
  const v = target.meta.vocab;
  const num = (f, value, placeholder, extra = "") =>
    `<input class="inline-num" type="number" inputmode="numeric" data-k="${k}" data-num="${f}" value="${value ?? ""}" placeholder="${placeholder}" ${extra}>`;
  const body = {
    firm_type: () => pills(k, "types", v.firm_types, c.types, TYPE_NAMES) + `<label class="inline muted">+ ${num("name_points", c.name_points, "0", 'aria-label="Points when the firm name says it"')} pts when the firm's name says it</label>`,
    rental_book: () => `<label class="inline">At least ${num("min", c.min, "any", 'min="0"')}</label><label class="inline">at most ${num("max", c.max, "any", 'min="0"')}</label><span class="unit">homes for rent</span><span class="muted">Leave both empty and a bigger book simply earns more, up to the points.</span>`,
    short_lets: () => `<label class="inline">At least ${num("min", c.min, "any", 'min="0"')}</label><span class="unit">short-lets</span><span class="muted">Leave it empty and more short-lets earn more, up to the points.</span>`,
    register: () => `<span class="muted">Licensed members, matched to the firm across sources.</span>`,
    location: () => pills(k, "cities", v.cities, c.cities),
    role: () => pills(k, "roles", v.roles, c.roles, ROLE_NAMES),
    channel: () => seg(k, "value", [["email", "Email"], ["phone", "Phone"], ["either", "Either"]], c.value) + `<span class="muted">A contact you can't reach isn't a lead, so this is always a must-have.</span>`,
    mostly_sales: () => seg(k, "must", [[false, "Lose points"], [true, "Leave out"]], c.must) + `<span class="muted">Firms with more than twice as many homes for sale as for rent and short-let.</span>`,
    exclude_locations: () => pills(k, "cities", v.cities, c.cities),
  }[k]();
  const phrases = [...new Set(c.evidence.map((e) => e.phrase))];
  const fixedMust = k === "channel" || k === "exclude_locations";
  const must =
    k === "mostly_sales" ? "" : fixedMust ? '<span class="chip c-info">always a must</span>'
      : `<label class="must"><input type="checkbox" data-k="${k}" data-flag="must" ${c.must ? "checked" : ""}> Must have</label>`;
  const pts = k === "exclude_locations" ? "" : `<label class="pts-in">${num("points", c.points, "0", `aria-label="Points for ${esc(label)}"`)}<span>pts</span></label>`;
  return `<div class="crit ${c.on ? "on" : "off"}" data-row="${k}">
    <div class="crit-head">
      <label class="switch"><input type="checkbox" data-k="${k}" data-flag="on" ${c.on ? "checked" : ""} aria-label="Use ${esc(label)}"><span></span></label>
      <span class="crit-name">${esc(label)}</span>
      <span class="crit-side">${must}${pts}</span>
    </div>
    <div class="crit-body">${body}</div>
    <div class="crit-ev">${phrases.length ? `<span class="from">From your words</span>${phrases.map((x) => `<q>${esc(x)}</q>`).join("")}` : c.on ? "" : '<span class="muted">Not mentioned</span>'}${c.by_hand && c.on ? '<span class="chip c-held">set by hand</span>' : ""}</div>
  </div>`;
}

function leftOutCard() {
  const d = target.draft;
  if (!d.dropped.length) return "";
  return `<div class="card" style="order:4"><p class="label">Dropped: not in what you wrote</p><ul class="dropped">${d.dropped
    .map((x) => `<li><span class="chip c-bad">dropped</span><span><b>${esc(FIELD_NAMES[x.field] ?? x.field)}</b>${x.value ? ` ${esc(x.value)}` : ""}: ${esc(x.why)}</span></li>`)
    .join("")}</ul></div>`;
}

function versionsCard() {
  const m = target.meta;
  return `<div class="card" style="order:5"><p class="label">Versions</p><ul class="versions">${m.versions
    .map(
      (v) => `<li><span class="vnum">v${v.version}</span><span class="vtext"><span class="vdesc">${esc(v.description)}</span><span class="sub">${esc(v.parser)} · ${esc(v.created_at.replace("T", " ").slice(0, 16))} UTC</span></span>
      ${v.version === m.version ? '<span class="chip c-ok">in use</span>' : `<button class="btn btn-ghost btn-sm" type="button" data-load="${v.version}">Load</button>`}</li>`,
    )
    .join("")}</ul></div>`;
}

function formStatus() {
  const m = target.meta;
  const running = state.job.state === "running";
  const stale = state.has_run && m.scored_with && m.scored_with !== m.version;
  const help = !state.has_run
    ? "Run the pipeline once. After that, rescoring takes seconds because nothing is fetched again."
    : target.dirty ? "Save first. Rescore always uses the saved version."
    : stale ? `Your contacts were scored with version ${m.scored_with}. Rescore to apply version ${m.version}.`
    : "Rescore re-runs only the score stage. Nothing is fetched or looked up again.";
  return `
    <button class="btn btn-dark" id="save-btn" type="button" ${target.dirty ? "" : "disabled"}>Save as version ${m.versions[0].version + 1}</button>
    <button class="btn btn-accent" id="rescore-btn" type="button" ${!state.has_run || target.dirty || running ? "disabled" : ""}>Rescore contacts</button>
    <p class="control-help">${esc(help)}</p>`;
}

function drawTarget() {
  const { meta, draft } = target;
  view.innerHTML = `
  <div class="section-head"><h2>Target</h2><span class="mono muted">ICP version ${meta.version}${state.has_run && meta.scored_with ? ` · contacts scored with version ${meta.scored_with}` : ""}</span></div>
  <p class="lede">Say who the list is for in plain English. The parser turns it into criteria you can check and change, and every criterion quotes the words it came from. Anything it can't find in what you wrote is dropped and listed, never guessed.</p>
  <div class="target-grid">
    <div class="stack">
      <div class="card describe" style="order:1">
        <label class="label" for="icp-text">Describe your ideal customer</label>
        <textarea id="icp-text" rows="5" maxlength="2000" spellcheck="true">${esc(target.text)}</textarea>
        <div class="examples"><span class="muted">Try one</span>${meta.examples.map((e, i) => `<button type="button" class="example" data-ex="${i}">${esc(e.title)}</button>`).join("")}</div>
        <div class="control-row">
          <button class="btn btn-accent" id="read-btn" type="button">Read it</button>
          <span class="mode ${meta.parser === "claude" ? "claude" : ""}">${meta.parser === "claude" ? "Claude parser" : "Baseline parser"}</span>
          ${target.cost ? `<span class="mono muted">$${(target.cost / 1e6).toFixed(4)}</span>` : ""}
          <p class="control-help" id="text-hint">${target.text !== draft.description ? "The description has changed. Press Read it to update the criteria." : meta.parser === "claude" ? "Claude reads it with a JSON schema. The server still checks every phrase." : "The baseline knows this directory's words. Set LEAD_ENGINE_MODE=anthropic and a key to read it with Claude instead."}</p>
        </div>
        ${target.note ? `<p class="notice held">${esc(target.note)}</p>` : ""}
      </div>
      ${leftOutCard()}
      ${versionsCard()}
    </div>
    <div class="stack">
      <div class="card form-card" style="order:2">
        <div class="section-head"><h3>Criteria</h3><span id="form-chip">${target.dirty ? '<span class="chip c-held">draft, not saved</span>' : `<span class="chip c-ok">version ${meta.version}, in use</span>`}</span></div>
        <p class="muted small">Read by ${esc(draft.parser || "hand")}. Points are yours to change. A failed must-have puts the contact outside the ICP: it drops to tier 3 and stays out of the export.</p>
        <div class="crit-list" id="crit-list">${meta.order.map(critRow).join("")}</div>
        <div class="tiers">Tier 1 from <input class="inline-num" type="number" data-tier="tier1" value="${draft.tiers.tier1}" aria-label="Tier 1 threshold"> points, tier 2 from <input class="inline-num" type="number" data-tier="tier2" value="${draft.tiers.tier2}" aria-label="Tier 2 threshold"></div>
        <div class="control-row" id="form-actions">${formStatus()}</div>
      </div>
      <div class="card" id="coverage" style="order:3">${coverageHtml(target.coverage)}</div>
    </div>
  </div>`;
  wireTarget();
}

function edited(k, redraw) {
  target.dirty = true;
  if (k && redraw) {
    const row = view.querySelector(`[data-row="${k}"]`);
    if (row) row.outerHTML = critRow(k);
  }
  $("#form-chip").innerHTML = '<span class="chip c-held">draft, not saved</span>';
  $("#form-actions").innerHTML = formStatus();
  wireActions();
  refreshCoverage();
}

const refreshCoverage = debounce(async () => {
  try {
    target.coverage = await post("/api/icp/coverage", { icp: target.draft });
    const box = $("#coverage");
    if (box) box.innerHTML = coverageHtml(target.coverage);
  } catch (e) {
    toast(e.message);
  }
}, 200);

function wireActions() {
  const save = $("#save-btn");
  if (save)
    save.onclick = async () => {
      try {
        const meta = await post("/api/icp", { icp: target.draft });
        target = { meta, draft: meta.icp, dirty: false, coverage: meta.coverage, note: "", cost: 0, text: meta.icp.description };
        await refresh();
        drawTarget();
        toast(state.has_run ? `Saved as version ${meta.version}. Rescore to apply it to your contacts.` : `Saved as version ${meta.version}.`);
      } catch (e) {
        toast(e.message);
      }
    };
  const rescore = $("#rescore-btn");
  if (rescore) rescore.onclick = () => startJob("/api/jobs/rescore", { pace });
}

function wireTarget() {
  const text = $("#icp-text");
  text.oninput = () => {
    target.text = text.value;
    $("#text-hint").textContent = target.text !== target.draft.description ? "The description has changed. Press Read it to update the criteria." : "";
  };
  view.querySelectorAll("[data-ex]").forEach((b) => (b.onclick = () => {
    target.text = target.meta.examples[Number(b.dataset.ex)].text;
    text.value = target.text;
    read();
  }));
  $("#read-btn").onclick = read;
  view.querySelectorAll("[data-load]").forEach((b) => (b.onclick = async () => {
    const v = await api(`/api/icp/versions/${b.dataset.load}`);
    target.draft = v.icp;
    target.text = v.icp.description;
    target.dirty = true;
    target.note = `Loaded version ${v.version}. Save it to make it the one in use.`;
    target.coverage = await post("/api/icp/coverage", { icp: v.icp });
    drawTarget();
  }));
  const list = $("#crit-list");
  list.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-k]");
    if (!b) return;
    const c = target.draft.criteria[b.dataset.k];
    if (b.dataset.list) {
      const arr = c[b.dataset.list];
      const i = arr.indexOf(b.dataset.v);
      i >= 0 ? arr.splice(i, 1) : arr.push(b.dataset.v);
      c.on = arr.length > 0;
      c.by_hand = true;
    } else if (b.dataset.set) {
      c[b.dataset.set] = b.dataset.set === "must" ? b.dataset.v === "true" : b.dataset.v;
      c.by_hand = true;
    }
    edited(b.dataset.k, true);
  });
  list.addEventListener("change", (e) => {
    const el = e.target;
    const k = el.dataset.k;
    if (!k) return;
    const c = target.draft.criteria[k];
    if (el.dataset.flag) {
      c[el.dataset.flag] = el.checked;
      if (el.dataset.flag === "must") c.by_hand = true;
      edited(k, true);
    } else if (el.dataset.num) {
      const f = el.dataset.num;
      const n = el.value === "" ? null : Math.round(Number(el.value));
      c[f] = f === "points" || f === "name_points" ? (n ?? 0) : n;
      if (f === "min" || f === "max") c.by_hand = true;
      edited(k, f === "min" || f === "max");
    }
  });
  view.querySelectorAll("[data-tier]").forEach((el) => (el.onchange = () => {
    target.draft.tiers[el.dataset.tier] = Math.round(Number(el.value) || 0);
    edited(null, false);
  }));
  wireActions();
}

async function read() {
  const description = ($("#icp-text")?.value ?? target.text).trim();
  if (!description) return toast("Write a description first.");
  const btn = $("#read-btn");
  btn.disabled = true;
  btn.textContent = "Reading…";
  try {
    const r = await post("/api/icp/parse", { description });
    target.draft = r.icp;
    target.text = r.icp.description;
    target.coverage = r.coverage;
    target.cost = r.cost_micros;
    target.note = r.icp.note ?? "";
    target.dirty = true;
    drawTarget();
    view.querySelectorAll(".crit.on").forEach((el, i) => el.style.setProperty("--i", i));
    view.querySelector("#crit-list")?.classList.add("arrive");
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
    btn.textContent = "Read it";
  }
}

function coverageHtml(cov) {
  const status = { covered: '<span class="chip c-ok">covered</span>', gap: '<span class="chip c-held">gap</span>', unsupported: '<span class="chip c-bad">no source</span>' };
  const notices = [
    cov.blocked ? `<li class="notice bad">${esc(cov.blocked)}</li>` : "",
    ...cov.warnings.map((w) => `<li class="notice held">${esc(w)}</li>`),
  ].join("");
  const best = cov.suggestions.filter((s) => s.covers.length);
  return `<div class="section-head"><h3>What your sources can give this ICP</h3><a class="mono muted" href="#sources">Manage sources</a></div>
    <ul class="notices">${notices || '<li class="notice ok">Every criterion is covered by a source that is on.</li>'}</ul>
    ${cov.rows.length ? `<div class="table-wrap"><table><thead><tr><th>Criterion</th><th>Sources that give it</th><th></th></tr></thead><tbody>${cov.rows
      .map((r) => `<tr><td><b>${esc(r.label)}</b>${r.must ? ' <span class="chip c-info">must</span>' : ""}<span class="sub">needs ${esc(r.cap_label)}</span></td>
        <td>${r.providers.length ? r.providers.map((k) => `<span class="chip ${r.live.includes(k) ? "c-ok" : "c-muted"}">${esc(SOURCE_NAMES[k])}${r.live.includes(k) ? "" : ": off"}</span>`).join(" ") : '<span class="muted">none in the catalog</span>'}</td>
        <td>${status[r.status]}</td></tr>`)
      .join("")}</tbody></table></div>` : '<p class="muted">Switch a criterion on to see which sources can give it.</p>'}
    ${best.length ? `<p class="label" style="margin-top:14px">Best sources for this ICP</p><ol class="ranked">${best
      .map((s) => `<li><span><b>${esc(s.name)}</b> covers ${s.covers.length} of ${cov.of}${s.enabled ? "" : ' <span class="chip c-held">off</span>'}</span><span class="sub">${esc(s.covers.join(", "))}</span></li>`)
      .join("")}</ol>` : ""}`;
}

// ---------------------------------------------------------------- Sources
function sourceCard(s, robots) {
  const h = s.health;
  const running = state.job.state === "running";
  const status = s.reading
    ? '<span class="chip c-ok">being read</span>'
    : s.enabled && s.requires ? `<span class="chip c-held">needs the ${esc(SOURCE_NAMES[s.requires])}</span>` : '<span class="chip c-muted">off</span>';
  const partial = s.capabilities.filter((c) => c.partial);
  const layouts = Object.entries(h.layouts);
  return `<article class="source ${s.reading ? "" : "is-off"}">
    <div class="source-head">
      <div><h3>${esc(s.name)}</h3><p class="muted">${esc(s.what)}</p></div>
      <label class="switch"><input type="checkbox" data-src="${s.key}" ${s.enabled ? "checked" : ""} ${running ? "disabled" : ""} aria-label="Read ${esc(s.name)}"><span></span></label>
    </div>
    <div class="row">${status}${s.robots_allowed ? '<span class="chip c-ok">robots.txt allows it</span>' : '<span class="chip c-bad">robots.txt disallows it</span>'}</div>
    <div class="block"><p class="label">Can give</p><div class="caps">${s.capabilities
      .map((c) => `<span class="chip ${c.needed ? "c-ok need" : "c-muted"}" title="${c.needed ? "Your ICP uses this" : "Not used by your ICP"}">${esc(c.label)}${c.partial ? "*" : ""}</span>`)
      .join("")}</div>${partial.length ? `<p class="small muted">${partial.map((c) => `*${esc(c.label)}: ${esc(c.partial)}`).join(". ")}.</p>` : ""}</div>
    <div class="block rules"><p class="label">Rules</p>
      <label class="rule"><span>Crawl delay<span class="sub">at least ${robots.crawl_delay} s, from robots.txt</span></span><span class="rule-in"><input class="inline-num" type="number" step="0.5" min="${robots.crawl_delay}" data-rule="crawl_delay" data-src="${s.key}" value="${s.crawl_delay ?? ""}" placeholder="${robots.crawl_delay}" ${running ? "disabled" : ""}><span>s</span></span></label>
      <label class="rule"><span>Max pages<span class="sub">empty reads every page</span></span><span class="rule-in"><input class="inline-num" type="number" min="1" data-rule="max_pages" data-src="${s.key}" value="${s.max_pages ?? ""}" placeholder="all" ${running ? "disabled" : ""}><span></span></span></label>
    </div>
    <div class="block"><p class="label">Last run</p>
      <dl class="stats">
        <div><dt>fetched</dt><dd>${h.fetched}</dd></div>
        <div><dt>failed</dt><dd class="${h.failed ? "bad" : ""}">${h.failed}</dd></div>
        <div><dt>retried</dt><dd class="${h.retried ? "held" : ""}">${h.retried}</dd></div>
        <div><dt>kept</dt><dd class="${h.shown !== h.kept ? "bad" : ""}">${h.kept}<small> of ${h.shown}</small></dd></div>
      </dl>
      ${layouts.length > 1 ? `<p class="small muted">Kept by layout: ${layouts.map(([l, n]) => `${esc(l)} ${n}`).join(", ")}</p>` : ""}
      ${h.last_problem ? `<p class="last-problem ${h.last_problem.level === "error" ? "bad" : "held"}">${esc(h.last_problem.message)}</p>` : '<p class="small muted">No problems on record.</p>'}
    </div>
  </article>`;
}

async function renderSources() {
  const [data, pages] = await Promise.all([api("/api/sources"), api("/api/pages").catch(() => [])]);
  if (currentTab() !== "sources") return;
  const reading = data.sources.filter((s) => s.reading).length;
  const status = (p) =>
    p.status === "disallowed" ? '<span class="chip c-held">skipped: robots.txt</span>' : p.status === "failed" ? '<span class="chip c-bad">failed</span>' : '<span class="chip c-ok">fetched</span>';
  view.innerHTML = `
  <div class="section-head"><h2>Sources</h2><span class="mono muted">${data.sources.length} in the catalog · ${reading} being read</span></div>
  <p class="lede">The directories the engine knows how to read. Switch one off and the next run sets its listings aside without fetching it. Switch it back on and it is read again from scratch. Each card shows what the source can give your ICP and how it behaved on the last run.</p>
  ${data.coverage.blocked ? `<p class="notice bad">${esc(data.coverage.blocked)}</p>` : ""}
  <section class="catalog">${data.sources.map((s) => sourceCard(s, data.robots)).join("")}</section>
  <section class="two-col">
    <div class="card" id="coverage">${coverageHtml(data.coverage)}</div>
    <div class="card no-web">
      <p class="label">Why there is no "search the web" button</p>
      <h3>Every field has to point at a page we can show you.</h3>
      <ul class="reasons-list">
        <li><b>Grounding.</b> A contact detail is only kept when it quotes a page in this catalog. Search results and scraped snippets can't be checked that way.</li>
        <li><b>Robots and terms.</b> Each source is crawled under its own robots.txt, delay and page limits, checked before it is added. The open web can't be checked like that, site by site.</li>
        <li><b>Quality and de-duplication.</b> A known layout is what lets the engine count listings shown against kept, read surnames the right way round, and link the same firm across sources.</li>
      </ul>
      <p class="small muted">Adding a source means adding a parser to this catalog, with its layouts, its rules and its tests.</p>
    </div>
  </section>
  <section class="two-col">
    <div class="card"><p class="label">robots.txt</p><pre class="mono" style="margin:8px 0 0;white-space:pre-wrap">${esc(data.robots.text || "(generate a directory first)")}</pre>
      <p class="muted" style="margin-top:8px">The crawler reads this first, never fetches a disallowed path, and never waits less than its crawl delay.</p></div>
    <div class="card"><p class="label">Open the raw directory</p><div class="control-row" style="margin-top:8px">
      <a class="btn btn-ghost btn-sm" href="/site/portal/page-1.html" target="_blank" rel="noopener">Portal page 1</a>
      <a class="btn btn-ghost btn-sm" href="/site/register/page-1.html" target="_blank" rel="noopener">Register page 1</a>
      <a class="btn btn-ghost btn-sm" href="/site/portal/index.html" target="_blank" rel="noopener">Portal index</a></div>
      <p class="muted" style="margin-top:8px">The same mess real directories have: two card layouts, surname-first capitals, phones without the leading zero, emails written as "[at]".</p></div>
  </section>
  <div class="section-head"><h3>Every page the crawler touched</h3><span class="mono muted">${pages.length} pages</span></div>
  ${pages.length ? `<div class="table-wrap"><table><thead><tr><th>Page</th><th>Source</th><th>Status</th><th>Attempts</th><th>Shown</th><th>Kept</th><th></th></tr></thead><tbody>${pages
    .map(
      (p) => `<tr><td class="mono">${esc(p.url)}</td><td>${esc(p.source)}</td><td>${status(p)}</td>
      <td class="num">${p.attempts ?? ""}${p.attempts > 1 ? ' <span class="chip c-held">retried</span>' : ""}</td>
      <td class="num">${p.seen ?? ""}</td><td class="num">${p.kept ?? ""}</td>
      <td>${p.seen == null ? "" : p.seen === p.kept ? '<span class="chip c-ok">all kept</span>' : '<span class="chip c-bad">gap</span>'} ${p.status === "ok" ? `<a class="mono muted" href="/site/${esc(p.url)}" target="_blank" rel="noopener">open</a>` : ""}</td></tr>`,
    )
    .join("")}</tbody></table></div>` : '<p class="empty">Run the pipeline to see the pages it fetched.</p>'}`;

  view.querySelectorAll("input[type=checkbox][data-src]").forEach((el) => (el.onchange = () => updateSource(el.dataset.src, { enabled: el.checked })));
  view.querySelectorAll("input[data-rule]").forEach((el) => (el.onchange = () => {
    const value = el.value === "" ? null : Number(el.value);
    updateSource(el.dataset.src, { [el.dataset.rule]: value });
  }));
}

async function updateSource(key, body) {
  try {
    const r = await post(`/api/sources/${key}`, body);
    await refresh();
    if (target && !target.dirty) target = null; // coverage on the Target tab is recomputed next time it opens
    await renderSources();
    if (r.note) toast(r.note);
    else if ("enabled" in body) toast(`${SOURCE_NAMES[key]} switched ${body.enabled ? "on. The next run reads it." : "off. The next run sets its listings aside."}`);
    else toast("Saved. The next run uses it.");
  } catch (e) {
    toast(e.message);
    renderSources();
  }
}

// ---------------------------------------------------------------- Evals
function renderEvalsLive() {
  const job = state.job;
  const box = $("#eval-status");
  if (!box) return;
  // When the eval job finishes, onJob re-renders the whole tab; this only draws the live status.
  if (job.kind === "eval" && job.state === "running") box.innerHTML = `<div class="status running"><span class="pulse"></span><span>${esc(job.phase || "Starting the evals…")} · ${job.seconds}s</span></div>`;
  else if (job.kind === "eval" && job.state === "failed") box.innerHTML = `<div class="status failed">${esc(job.message)}</div>`;
  const b = $("#eval-btn");
  if (b) b.disabled = job.state === "running";
}

function renderEvals() {
  const ev = state.job.kind === "eval" && state.job.result ? state.job.result : state.evals;
  const running = state.job.state === "running";
  const m = ev?.matching;
  view.innerHTML = `
  <div class="section-head"><h2>Evals</h2><button class="btn btn-accent" id="eval-btn" ${running ? "disabled" : ""}>${ev ? "Run the evals again" : "Run the evals"}</button></div>
  <div id="eval-status"></div>
  <p class="lede">The parts that make judgement calls are measured against known answers, so a change to a rule or a model is judged on numbers, not impressions. The matching eval runs the whole pipeline on a fresh directory and checks every pair of listings against the truth file.</p>
  ${
    !ev
      ? '<p class="empty">No results yet. Press Run the evals (takes a few seconds).</p>'
      : `
  <h3 style="font-size:18px">Matching, end to end</h3>
  <div class="tiles">
    ${tile(m.link_precision, `automatic links that were right (${m.wrong_links} wrong merges)`, m.wrong_links ? "bad" : "ok")}
    ${tile(m.link_recall, `true duplicates linked automatically (${m.linked_true_pairs} of ${m.true_pairs})`)}
    ${tile(m.review_precision, `review pairs that really were the same (${m.review_true} of ${m.review_total})`, "held")}
    ${tile(m.surfaced, `true duplicates surfaced: linked or sent to review (${m.surfaced_n} of ${m.true_pairs})`, "ok")}
  </div>
  <h3 style="font-size:18px">Extraction</h3>
  <p class="muted">Precision / recall per field. A field only counts if its quote is really on the page.${ev.claude ? "" : " Set LEAD_ENGINE_MODE=anthropic and an API key to add Claude's row."}</p>
  <div class="table-wrap"><table><thead><tr><th>Extractor</th><th>Set</th><th>Name</th><th>Phones</th><th>Emails</th><th>Dropped</th><th>Cost</th></tr></thead><tbody>
    ${ev.extraction.map((r) => `<tr><td>${esc(r.extractor)}</td><td>${esc(r.set)} (${r.n})</td><td class="num">${esc(r.name)}</td><td class="num">${esc(r.phones)}</td><td class="num">${esc(r.emails)}</td><td class="num">${r.dropped}</td><td class="num">${esc(r.cost)}</td></tr>`).join("")}
  </tbody></table></div>
  <h3 style="font-size:18px">Name matching traps</h3>
  <div class="tiles">${tile(ev.names.precision, "precision")}${tile(ev.names.recall, "recall")}${tile(`${ev.names.kept_apart} of ${ev.names.different_total}`, "different people correctly kept apart")}</div>
  ${ev.names.misses.length ? `<div class="card"><p class="label">Reported, not hidden</p><ul class="decided">${ev.names.misses.map((x) => `<li>${esc(x.replace(/^- /, "").replace(/\*\*/g, ""))}</li>`).join("")}</ul></div>` : ""}`
  }`;
  $("#eval-btn").onclick = () => startJob("/api/jobs/eval", {}, "evals");
  renderEvalsLive();
}

function emptyRun(msg) {
  view.innerHTML = `<div class="empty"><p>${esc(msg)} Run the pipeline first.</p><p style="margin-top:12px"><a class="btn btn-accent" href="#run">Go to Run</a></p></div>`;
}

// ---------------------------------------------------------------- routing
function currentTab() {
  return (location.hash || "#run").slice(1);
}

async function route() {
  const tab = currentTab();
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
  await refresh();
  connectStream();
  ({ target: renderTarget, sources: renderSources, run: renderRun, contacts: renderContacts, review: renderReview, evals: renderEvals }[tab] ?? renderRun)();
}

function debounce(fn, ms) {
  let t;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}

// New directory dialog
$("#new-directory").onclick = () => $("#dir-dialog").showModal();
$("#dir-form").addEventListener("submit", (e) => {
  const action = e.submitter?.value;
  if (action === "go") {
    startJob("/api/jobs/generate", { size: Number($("#dir-size").value), seed: Number($("#dir-seed").value) });
  }
});

window.addEventListener("hashchange", route);
route();
