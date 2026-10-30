const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

const S = { sid: null, state: null, step: 0, quiz: {}, edits: {}, original: {}, current: null, events: [], t0: 0, usage: [], timer: null };
const log = (e) => S.events.push({ t: Math.round((Date.now() - S.t0) / 1000), ...e });

/* ---------- streaming helper ---------- */
async function stream(url, body, onEvent) {
  const res = await post(url, body);
  if (!res.ok) throw new Error((await res.json()).detail || `Error ${res.status}`);
  const reader = res.body.getReader(), dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const line = buf.slice(0, i).replace(/^data: /, ""); buf = buf.slice(i + 2);
      if (line.trim()) onEvent(JSON.parse(line));
    }
  }
}
function mark(listSel, ev) {
  const li = document.querySelector(`${listSel} li[data-step="${ev.step}"]`);
  if (!li) return;
  li.className = ev.status;
  if (ev.status === "running") li.dataset.t0 = performance.now();
  if (ev.status === "done") {
    li.querySelector(".t")?.remove();
    const t = document.createElement("span"); t.className = "t";
    t.textContent = `${((performance.now() - li.dataset.t0) / 1000).toFixed(1)}s`; li.appendChild(t);
  }
}
function phase(name) {
  ["start", "learn", "challenge", "results"].forEach((p) => $(`#${p}`).classList.toggle("hidden", p !== name));
  const order = ["start", "learn", "challenge", "results"];
  document.querySelectorAll("#phases span").forEach((s) => {
    s.classList.toggle("on", s.dataset.phase === name);
    s.classList.toggle("done", order.indexOf(s.dataset.phase) < order.indexOf(name));
  });
  setTimeout(() => { learnCM.refresh(); challengeCM.refresh(); }, 30);
}

/* ---------- editors ---------- */
const cmOpts = { mode: "python", lineNumbers: true, indentUnit: 4, viewportMargin: 50 };
const learnCM = CodeMirror($("#learnEditor"), { ...cmOpts, readOnly: true });
const challengeCM = CodeMirror($("#challengeEditor"), { ...cmOpts });
let hl = null;
function showIn(cm, content, line) {
  cm.setValue(content);
  if (hl != null) cm.removeLineClass(hl, "background", "hl-line");
  if (line) {
    hl = line - 1;
    cm.addLineClass(hl, "background", "hl-line");
    cm.scrollIntoView({ line: hl, ch: 0 }, 160);
  }
}
async function fetchFile(path) {
  if (S.original[path] == null) {
    const r = await fetch(`/api/file?sid=${S.sid}&path=${encodeURIComponent(path)}`);
    if (!r.ok) throw new Error("Couldn't open that file");
    S.original[path] = (await r.json()).content;
  }
  return S.original[path];
}

/* ---------- start ---------- */
fetch("/api/meta").then((r) => r.json()).then((m) => {
  $("#runner").textContent = m.runner === "nebius-sandbox" ? "Tests run in Nebius Sandboxes" : "Tests run in a local runner";
  $("#baselineChip").textContent = m.runner === "nebius-sandbox" ? "Nebius Sandbox" : "Test runner";
  $("#curated").innerHTML = Object.entries(m.curated).map(([slug, c]) =>
    `<button class="repo-card" data-slug="${esc(slug)}"><b>${esc(slug)}</b><span>${esc(c.blurb)}</span></button>`).join("");
  document.querySelectorAll(".repo-card").forEach((b) => b.addEventListener("click", () => begin(b.dataset.slug)));
});
$("#repoForm").addEventListener("submit", (e) => { e.preventDefault(); if ($("#repoInput").value.trim()) begin($("#repoInput").value.trim()); });

async function begin(repo) {
  $("#startError").classList.add("hidden");
  document.querySelectorAll("#setupSteps li").forEach((li) => { li.className = ""; li.querySelector(".t")?.remove(); });
  document.querySelectorAll(".repo-card, #startBtn").forEach((b) => (b.disabled = true));
  try {
    await stream("/api/start", { repo }, (ev) => {
      if (ev.status === "error") throw new Error(ev.message);
      mark("#setupSteps", ev);
      if (ev.step === "ready") ready(ev.result);
    });
  } catch (err) {
    $("#startError").textContent = err.message; $("#startError").classList.remove("hidden");
  }
  document.querySelectorAll(".repo-card, #startBtn").forEach((b) => (b.disabled = false));
}

function ready(state) {
  Object.assign(S, { sid: state.session, state, step: 0, quiz: {}, edits: {}, original: {}, current: null, events: [], usage: state.usage });
  $("#tourSummary").textContent = state.tour.summary;
  renderStep();
  phase("learn");
}

/* ---------- learn ---------- */
async function renderStep() {
  const steps = S.state.tour.steps, st = steps[S.step];
  $("#tourCount").textContent = `Step ${S.step + 1} of ${steps.length}`;
  $("#prevStep").disabled = S.step === 0;
  $("#nextStep").textContent = S.step === steps.length - 1 ? "Start the challenge →" : "Next →";
  const q = st.quiz;
  $("#tourStep").innerHTML = `<h2>${esc(st.title)}</h2>
    <div class="syms">${st.files.map((f) => `<span class="sym" data-path="${esc(f)}">${esc(f)}</span>`).join("")}
      ${st.symbols.filter((s) => s.path).map((s) => `<span class="sym" data-path="${esc(s.path)}" data-line="${s.line}">${esc(s.name)}()</span>`).join("")}</div>
    <p>${esc(st.explain)}</p>
    ${st.look_for ? `<div class="look">👀 ${esc(st.look_for)}</div>` : ""}
    ${q ? `<div class="quiz"><b>${esc(q.q)}</b>${q.options.map((o, i) => `<button data-i="${i}">${esc(o)}</button>`).join("")}<div class="why hidden">${esc(q.why)}</div></div>` : ""}`;
  document.querySelectorAll("#tourStep .sym").forEach((el) => el.addEventListener("click", () => openLearn(el.dataset.path, +el.dataset.line || 0)));
  document.querySelectorAll("#tourStep .quiz button").forEach((b) => b.addEventListener("click", () => {
    if (S.quiz[S.step] != null) return;
    const right = +b.dataset.i === q.answer;
    S.quiz[S.step] = right;
    b.classList.add(right ? "right" : "wrong");
    document.querySelectorAll("#tourStep .quiz button")[q.answer].classList.add("right");
    $("#tourStep .why").classList.remove("hidden");
  }));
  const first = st.symbols.find((s) => s.path) || (st.files[0] && { path: st.files[0], line: 0 });
  if (first) openLearn(first.path, first.line);
}
async function openLearn(path, line) {
  if (!path.endsWith(".py")) return;
  $("#learnPath").textContent = path + (line ? `:${line}` : "");
  showIn(learnCM, await fetchFile(path), line);
}
$("#prevStep").addEventListener("click", () => { S.step--; renderStep(); });
$("#nextStep").addEventListener("click", () => {
  if (S.step < S.state.tour.steps.length - 1) { S.step++; renderStep(); } else startChallenge();
});
$("#skipTour").addEventListener("click", startChallenge);

/* ---------- challenge ---------- */
function startChallenge() {
  S.t0 = Date.now();
  $("#ticketTitle").textContent = S.state.ticket.title;
  $("#ticketBody").textContent = S.state.ticket.body;
  $("#tree").innerHTML = S.state.tree.map((p) => `<li class="${p.endsWith(".py") ? "py" : ""}" data-path="${esc(p)}" title="${esc(p)}">${esc(p)}</li>`).join("");
  document.querySelectorAll("#tree li.py").forEach((li) => li.addEventListener("click", () => openFile(li.dataset.path)));
  $("#chat").innerHTML = "";
  $("#testOut").classList.add("hidden");
  clearInterval(S.timer);
  S.timer = setInterval(() => {
    const s = Math.round((Date.now() - S.t0) / 1000);
    $("#timer").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }, 1000);
  phase("challenge");
  const firstSrc = S.state.tree.find((p) => p.endsWith(".py") && !/(^|\/)test/.test(p));
  if (firstSrc) openFile(firstSrc);
}
function saveCurrent() {
  if (!S.current) return;
  const v = challengeCM.getValue();
  if (v !== S.original[S.current]) S.edits[S.current] = v; else delete S.edits[S.current];
  document.querySelectorAll("#tree li").forEach((li) => li.classList.toggle("edited", li.dataset.path in S.edits));
}
async function openFile(path) {
  saveCurrent();
  const content = S.edits[path] ?? (await fetchFile(path));
  S.current = path;
  $("#challengePath").textContent = path;
  showIn(challengeCM, content, 0);
  document.querySelectorAll("#tree li").forEach((li) => li.classList.toggle("on", li.dataset.path === path));
  log({ open: path });
}
challengeCM.on("change", () => { clearTimeout(S.saveT); S.saveT = setTimeout(saveCurrent, 300); });

$("#runBtn").addEventListener("click", async () => {
  saveCurrent();
  $("#runBtn").disabled = true; $("#runBtn").textContent = "Running…";
  log({ run_tests: Object.keys(S.edits) });
  try {
    const r = await (await post("/api/run", { sid: S.sid, edits: S.edits })).json();
    const out = $("#testOut"); out.classList.remove("hidden");
    out.innerHTML = r.error ? `<span class="bad">Tests didn't run</span><pre>${esc(r.error)}</pre>`
      : `${r.failed ? `<span class="bad">✗ ${r.failed} failing</span>` : `<span class="ok">✓ all passing</span>`} · ${r.passed} passed · ${r.seconds}s on ${esc(r.runner)}
         ${r.failures.map((f) => `<pre>✗ ${esc(f)}\n${esc((r.details[f] || "").trim().split("\n").slice(-6).join("\n"))}</pre>`).join("")}`;
    log({ test_result: { failed: r.failed } });
  } catch (err) { alert(err.message); }
  $("#runBtn").disabled = false; $("#runBtn").textContent = "▶ Run tests";
});

$("#chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#chatInput").value.trim();
  if (!text) return;
  $("#chatInput").value = "";
  addMsg("user", text);
  const thinking = addMsg("assistant thinking", "Thinking…");
  log({ ask: text.slice(0, 120), file: S.current });
  try {
    const r = await (await post("/api/chat", { sid: S.sid, message: text, path: S.current })).json();
    thinking.remove();
    const m = addMsg("assistant", r.text);
    const flag = document.createElement("button");
    flag.className = "flag"; flag.textContent = "⚑ This looks wrong";
    flag.addEventListener("click", async () => {
      if (flag.classList.contains("on")) return;
      flag.classList.add("on"); flag.textContent = "⚑ Flagged as wrong";
      log({ flagged: r.id });
      await post("/api/flag", { sid: S.sid, id: r.id });
    });
    m.appendChild(flag);
  } catch (err) { thinking.textContent = err.message; }
});
$("#chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chatForm").requestSubmit(); } });
const md = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/^#+\s*/gm, "");
function addMsg(cls, text) {
  const d = document.createElement("div"); d.className = `msg ${cls}`; d.innerHTML = md(text);
  $("#chat").appendChild(d); $("#chat").scrollTop = 1e9; return d;
}

/* ---------- submit & results ---------- */
$("#submitBtn").addEventListener("click", async () => {
  saveCurrent();
  if (!Object.keys(S.edits).length && !confirm("You haven't changed any code. Submit anyway?")) return;
  clearInterval(S.timer);
  phase("results");
  $("#resultBody").classList.add("hidden");
  document.querySelectorAll("#scoreSteps li").forEach((li) => { li.className = ""; li.querySelector(".t")?.remove(); });
  const quizVals = Object.values(S.quiz);
  const R = {};
  try {
    await stream("/api/submit", { sid: S.sid, edits: S.edits, events: S.events, quiz: { correct: quizVals.filter(Boolean).length, answered: quizVals.length } }, (ev) => {
      if (ev.status === "error") throw new Error(ev.message);
      mark("#scoreSteps", ev);
      if (ev.status === "done") R[ev.step] = ev.result;
      if (ev.step === "complete") renderResults(R);
    });
  } catch (err) { alert(err.message); }
});

function renderResults(R) {
  const card = R.score, done = R.complete, tests = R.tests, hidden = R.hidden, blast = R.blast;
  $("#signal").innerHTML = `${esc(card.signal)}<small>practice signal</small>`;
  $("#scoreSummary").textContent = card.summary;
  $("#scoreNext").textContent = card.next_practice ? `Next: ${card.next_practice}` : "";
  const fixed = tests.failed === 0 && !tests.error;
  $("#facts").innerHTML = [
    [fixed ? "✓" : "✗", fixed ? "All tests pass" : `${tests.failed} tests still failing`, fixed],
    [hidden.kept ? `${hidden.passed}/${hidden.kept}` : "–", "Hidden edge-case tests passed", hidden.kept && hidden.passed === hidden.kept],
    [done.trap ? (done.trap.flagged ? "Caught" : "Missed") : "–", "Assistant's wrong answer", done.trap?.flagged],
    [`${card.facts.minutes} min`, "Time to submit", null],
  ].map(([v, l, g]) => `<div class="fact ${g === true ? "good" : g === false ? "bad" : ""}"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join("");
  $("#dims").innerHTML = (card.dimensions || []).map((d) => `<div class="dim"><h4>${esc(d.name)}<span class="pips">${[1, 2, 3, 4].map((i) => `<i class="${i <= d.score ? "on" : ""}"></i>`).join("")}</span></h4>
    <p>${esc(d.evidence)}</p><p class="tip">→ ${esc(d.tip)}</p></div>`).join("");
  const b = done.bug;
  $("#reveal").innerHTML = `<p class="kv"><code>${esc(b.path)}:${b.line}</code> in <code>${esc(b.function)}</code> · ${esc(b.bug_type)}</p>
    <div class="code">${lineDiff(b.replace, b.find)}</div><p class="kv">${esc(b.explanation)}</p>`;
  $("#trapReveal").innerHTML = done.trap
    ? `<p class="kv"><span class="pill ${done.trap.flagged ? "good" : "bad"}">${done.trap.flagged ? "You flagged it" : "You didn't flag it"}</span></p><p class="kv">${esc(done.trap.text)}</p>`
    : `<p class="kv">You asked the assistant fewer than two questions, so it never got the chance to mislead you.</p>`;
  $("#blast").innerHTML = `<p class="kv">Changed: ${blast.functions.map((f) => `<code>${esc(f)}</code>`).join(", ") || "nothing"}</p>
    <p class="kv">Files that import your change (directly or indirectly): ${blast.dependents.length ? blast.dependents.map((f) => `<code>${esc(f)}</code>`).join(", ") : "none"}</p>
    <p class="kv">All of them were covered by the full test run${hidden.kept ? ` plus ${hidden.kept} hidden edge-case test${hidden.kept > 1 ? "s" : ""} written against the original code` : ""}.</p>
    ${hidden.failed?.length ? `<p class="kv">Failed hidden tests: ${hidden.failed.map((n) => `<code>${esc(n)}</code>`).join(", ")}</p>` : ""}`;
  $("#diff").innerHTML = blast.diff ? esc(blast.diff).split("\n").map((l) =>
    `<span class="${l.startsWith("+") && !l.startsWith("+++") ? "a" : l.startsWith("-") && !l.startsWith("---") ? "d" : l.startsWith("@@") ? "h" : ""}">${l}</span>`).join("\n") : "No changes.";
  const usage = [...S.usage, ...done.usage];
  $("#routing").innerHTML = `<table><tr><th>Step</th><th>Model</th><th>Time</th><th>Tokens</th></tr>${usage.map((u) =>
    `<tr><td>${esc(u.step)}<div class="m">${esc(u.model)}</div></td><td><span class="chip ${u.tier}">${u.tier}</span></td><td>${u.latency_s}s</td><td>${u.prompt_tokens + u.completion_tokens}${u.reasoning_tokens ? ` <span class="m">(${u.reasoning_tokens} reasoning)</span>` : ""}</td></tr>`).join("")}</table>`;
  $("#resultBody").classList.remove("hidden");
}
function lineDiff(bad, good) {
  const a = bad.split("\n"), g = good.split("\n"), out = [];
  for (let i = 0; i < Math.max(a.length, g.length); i++) {
    if (a[i] === g[i]) continue;
    if (a[i] != null) out.push(`<del>- ${esc(a[i].trim())}</del>`);
    if (g[i] != null) out.push(`<ins>+ ${esc(g[i].trim())}</ins>`);
  }
  return out.join("\n");
}
$("#again").addEventListener("click", () => { phase("start"); window.scrollTo(0, 0); });
