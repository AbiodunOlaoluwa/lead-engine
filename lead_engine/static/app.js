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
  if (job.kind === "run" && job.state === "done" && job.result) state.summary = job.result; // final numbers arrive with the "done" message
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
    const pct = st.total ? Math.min(100, Math.round((st.done / st.total) * 100)) : cls === "done" ? 100 : 0;
    const count =
      fromLastRun || job.kind === "eval" || job.kind === "generate"
        ? state.has_run ? "done on the last run" : "waiting"
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

  const label = { run: "the pipeline", drill: "the crash drill", eval: "the evals", generate: "the generator" }[job.kind] ?? "";
  const text =
    job.state === "running"
      ? `Running ${label}${job.current ? `: ${job.current}` : ""} · ${job.seconds}s`
      : job.message || (state.has_run ? "Results from the last run are below. Run again, crash it, or explore the other tabs." : "Nothing has run yet. Press Run the pipeline.");
  $("#status").innerHTML = `<div class="status ${job.state}">${job.state === "running" ? '<span class="pulse"></span>' : ""}<span>${esc(text)}${job.phase ? `<span class="phase">${esc(job.phase)}</span>` : ""}</span></div>`;

  // Numbers only show once the engine says the run is finished; mid-run they would be half-true.
  const midRun = (job.state === "running" || job.state === "interrupted") && (job.kind === "run" || job.kind === "drill");
  $("#tiles").innerHTML = !state.has_run
    ? ""
    : midRun
      ? `<p class="partial">${job.state === "interrupted" ? "The run stopped part-way, so its totals would be half-true. Resume it and they appear here." : "Totals appear the moment the engine reports the run finished."}</p>`
      : summaryTiles(state.summary);
  for (const id of ["run-btn", "crash-btn", "drill-btn", "reset-btn"]) {
    const b = $("#" + id);
    if (b) b.disabled = job.state === "running";
  }
  const resume = $("#resume-btn");
  if (resume) resume.disabled = !(job.state === "interrupted" && job.kind === "run");

  const drill = $("#drill-card");
  if (job.kind === "drill" && job.state === "done" && job.result) {
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
  const filtered = rows.filter((r) =>
    contactFilter === "tier1" ? r.tier === 1 : contactFilter === "tier2" ? r.tier === 2 : contactFilter === "review" ? r.needs_review : contactFilter === "noemail" ? !r.email : true,
  );
  const held = rows.filter((r) => r.needs_review).length;
  view.innerHTML = `
  <div class="section-head"><h2>Contacts</h2><span class="mono muted">${rows.length} contacts from ${state.summary.listings} listings</span></div>
  <div class="toolbar">
    <div class="toolbar-left">
      <input class="search" id="q" type="search" placeholder="Search name, firm or city" value="${esc(contactQuery)}" aria-label="Search contacts">
      <div class="filters">
        ${[["all", "All"], ["tier1", "Tier 1"], ["tier2", "Tier 2"], ["review", "Waiting for review"], ["noemail", "No email"]]
          .map(([k, l]) => `<button class="filter ${contactFilter === k ? "on" : ""}" data-f="${k}">${l}</button>`)
          .join("")}
      </div>
    </div>
    <a class="btn btn-dark btn-sm" href="/api/export.csv">Export CSV · ${rows.length - held} ready${held ? `, ${held} held for review` : ""}</a>
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
          <td>${r.sources.map((s) => `<span class="chip c-info">${esc(s)}</span>`).join(" ")}${r.needs_review ? ' <span class="chip c-held">review</span>' : ""}</td>
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
    <div class="card"><div class="section-head"><h2 style="font-size:20px">${esc(d.name)}</h2>${tierChip(d.tier)}</div>
      <p class="muted" style="margin-top:4px">${d.listings.length} listing${d.listings.length === 1 ? "" : "s"} · ${d.score} points</p>
      <table style="margin-top:10px"><tbody>${d.breakdown.map((p) => `<tr><td class="pts ${p.points < 0 ? "neg" : ""}">${p.points > 0 ? "+" : ""}${p.points}</td><td>${esc(p.why)}</td></tr>`).join("")}</tbody></table>
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

// ---------------------------------------------------------------- Sources
async function renderSources() {
  const [pages, robots] = await Promise.all([api("/api/pages").catch(() => []), fetch("/site/robots.txt").then((r) => (r.ok ? r.text() : ""))]);
  const status = (p) =>
    p.status === "disallowed" ? '<span class="chip c-held">skipped: robots.txt</span>' : p.status === "failed" ? '<span class="chip c-bad">failed</span>' : '<span class="chip c-ok">fetched</span>';
  view.innerHTML = `
  <div class="section-head"><h2>Sources</h2><span class="mono muted">${pages.length} pages</span></div>
  <p class="lede">Every page the crawler touched, with how many listings it showed and how many the parser kept. A parser that skips a layout shows up here as a gap. The real one once dropped every premium listing, half of each page, without an error.</p>
  <div class="two-col">
    <div class="card"><p class="label">robots.txt</p><pre class="mono" style="margin:8px 0 0;white-space:pre-wrap">${esc(robots || "(generate a directory first)")}</pre>
      <p class="muted" style="margin-top:8px">The crawler reads this first, honours the delay and never fetches the disallowed path.</p></div>
    <div class="card"><p class="label">Open the raw directory</p><div class="control-row" style="margin-top:8px">
      <a class="btn btn-ghost btn-sm" href="/site/portal/page-1.html" target="_blank" rel="noopener">Portal page 1</a>
      <a class="btn btn-ghost btn-sm" href="/site/register/page-1.html" target="_blank" rel="noopener">Register page 1</a>
      <a class="btn btn-ghost btn-sm" href="/site/portal/index.html" target="_blank" rel="noopener">Portal index</a></div>
      <p class="muted" style="margin-top:8px">The same mess real directories have: two card layouts, surname-first capitals, phones without the leading zero, emails written as "[at]".</p></div>
  </div>
  ${pages.length ? `<div class="table-wrap"><table><thead><tr><th>Page</th><th>Source</th><th>Status</th><th>Attempts</th><th>Shown</th><th>Kept</th><th></th></tr></thead><tbody>${pages
    .map(
      (p) => `<tr><td class="mono">${esc(p.url)}</td><td>${esc(p.source)}</td><td>${status(p)}</td>
      <td class="num">${p.attempts ?? ""}${p.attempts > 1 ? ' <span class="chip c-held">retried</span>' : ""}</td>
      <td class="num">${p.seen ?? ""}</td><td class="num">${p.kept ?? ""}</td>
      <td>${p.seen == null ? "" : p.seen === p.kept ? '<span class="chip c-ok">all kept</span>' : '<span class="chip c-bad">gap</span>'} ${p.status === "ok" ? `<a class="mono muted" href="/site/${esc(p.url)}" target="_blank" rel="noopener">open</a>` : ""}</td></tr>`,
    )
    .join("")}</tbody></table></div>` : '<p class="empty">Run the pipeline to see the pages it fetched.</p>'}`;
}

// ---------------------------------------------------------------- Evals
function renderEvalsLive() {
  const job = state.job;
  const box = $("#eval-status");
  if (!box) return;
  if (job.state === "running") box.innerHTML = `<div class="status running"><span class="pulse"></span><span>${esc(job.phase || "Starting the evals…")} · ${job.seconds}s</span></div>`;
  else if (job.state === "done") renderEvals();
  else if (job.state === "failed") box.innerHTML = `<div class="status failed">${esc(job.message)}</div>`;
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
  ({ run: renderRun, contacts: renderContacts, review: renderReview, sources: renderSources, evals: renderEvals }[tab] ?? renderRun)();
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
