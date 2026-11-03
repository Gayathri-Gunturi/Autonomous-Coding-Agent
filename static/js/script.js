(() => {
  "use strict";
  const $ = (sel) => document.querySelector(sel);
  const TERMINAL = new Set(["passed", "failed", "error", "cancelled"]);
  const STAGE_LABEL = { pending: "Pending", running: "Running", completed: "Completed", failed: "Failed", skipped: "Skipped" };
  const SAMPLES = [
    ["Palindromes", "Write is_palindrome(text) that ignores case, spaces and punctuation, and longest_palindromic_substring(text) that returns the longest palindromic substring (first one on ties, empty string for empty input)."],
    ["LRU cache", "Implement an LRUCache class with get(key) returning -1 when missing and put(key, value), both O(1), evicting the least recently used item when capacity is exceeded. Capacity must be a positive integer."],
    ["CSV parser", "Write parse_csv_line(line) that splits one CSV line into fields, supporting double-quoted fields containing commas and escaped quotes (\"\"), and preserving empty fields."],
    ["Calculator", "Write evaluate(expression) that evaluates arithmetic with + - * / ^ parentheses, decimals and unary minus WITHOUT using eval, raising ValueError on malformed input and ZeroDivisionError on division by zero."],
  ];

  let taskId = null, pollTimer = null, dirty = false, logCount = 0, lastData = null, suppress = false;

  // ---------- helpers ----------
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") el.className = v; else if (k === "text") el.textContent = v; else el.setAttribute(k, v);
    }
    kids.flat().forEach((c) => c != null && el.append(c.nodeType ? c : document.createTextNode(c)));
    return el;
  }
  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    let body = null;
    try { body = await res.json(); } catch (_) { /* non-JSON */ }
    if (!res.ok || !body || body.ok === false) throw new Error((body && body.error && body.error.message) || `Request failed (${res.status})`);
    return body.data;
  }
  const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });
  function banner(msg, info) {
    const b = $("#banner");
    b.textContent = msg || ""; b.classList.toggle("hidden", !msg); b.classList.toggle("info", !!info);
  }
  function makeEditor(host, readOnly) {
    if (window.CodeMirror) {
      const cm = window.CodeMirror(host, { value: "", mode: "python", theme: "material-darker", lineNumbers: true,
        readOnly, indentUnit: 4, indentWithTabs: false, extraKeys: { Tab: (c) => c.replaceSelection("    ") } });
      return { get: () => cm.getValue(), set: (v) => cm.setValue(v), refresh: () => cm.refresh(), onChange: (fn) => cm.on("change", fn) };
    }
    const ta = h("textarea", { class: "fallback-editor", spellcheck: "false" });
    ta.readOnly = readOnly; host.append(ta);
    return { get: () => ta.value, set: (v) => { ta.value = v; }, refresh() {}, onChange: (fn) => ta.addEventListener("input", fn) };
  }
  const codeEd = makeEditor($("#paneCode"), false);
  const testsEd = makeEditor($("#paneTests"), true);
  codeEd.onChange(() => { if (!suppress) { dirty = true; updateButtons(); } });
  function setCode(ed, value) { suppress = true; ed.set(value || ""); suppress = false; }

  // ---------- rendering ----------
  function renderStages(stages, finished) {
    const ol = $("#stages"); ol.replaceChildren();
    let done = 0;
    stages.forEach((s) => {
      if (s.status === "completed" || s.status === "skipped") done++;
      const label = s.id === "debugging" && s.status === "pending" && !finished ? "Only if needed" : STAGE_LABEL[s.status];
      ol.append(h("li", { class: `stage ${s.status}` }, h("span", { class: "dot" }),
        h("div", {}, h("div", { text: s.label }), s.detail ? h("small", { text: s.detail }) : null),
        h("span", { class: `pill ${s.status}`, text: label })));
    });
    $("#progressBar").style.width = `${Math.round((done / stages.length) * 100)}%`;
  }
  function renderLogs(logs) {
    const box = $("#logs");
    if (logs.length < logCount) { box.replaceChildren(); logCount = 0; }
    const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    logs.slice(logCount).forEach((l) => box.append(h("div", { class: `log ${l.level}` },
      h("span", { class: "t", text: `[${l.time}] ` }), h("span", { class: "a", text: `${l.agent} › ${l.operation}: ` }), h("span", { class: "m", text: l.message }))));
    logCount = logs.length;
    if (stick) box.scrollTop = box.scrollHeight;
  }
  function renderTests(d) {
    const r = d.test_report, ex = d.execution;
    const badge = $("#testBadge");
    $("#cases").replaceChildren();
    if (!r) { badge.className = "pill pending"; badge.textContent = d.status === "running" ? "Waiting…" : "No run yet";
      ["Total", "Passed", "Failed", "Time"].forEach((n) => { $(`#stat${n}`).textContent = "–"; }); $("#errors").classList.add("hidden"); return; }
    $("#statTotal").textContent = r.total; $("#statPassed").textContent = r.passed_count;
    $("#statFailed").textContent = r.failed + r.errors; $("#statTime").textContent = `${(r.duration || 0).toFixed(2)}s`;
    const good = r.passed && (!ex || ex.ok);
    const running = d.status === "running" || d.status === "queued";
    badge.className = `pill ${good ? "passed" : running ? "running" : "failed"}`;
    badge.textContent = good ? (running ? "Passing (iteration)" : "All tests passed") : (running ? "Failing – debugging" : "Not passing");
    r.cases.forEach((c) => $("#cases").append(h("li", { class: c.status[0] }, c.status === "passed" ? "✔" : c.status === "skipped" ? "○" : "✘", c.name)));
    const parts = [];
    if (ex && !ex.ok) parts.push(`# solution.py execution (exit ${ex.exit_code}${ex.timed_out ? ", timed out" : ""})\n${ex.stderr || ex.blocked_reason || ""}`);
    if (r.harness_error) parts.push(`# test harness\n${r.harness_error}\n${r.stderr || ""}`);
    r.cases.filter((c) => c.status === "failed" || c.status === "error").forEach((c) => parts.push(`# ${c.name}\n${c.message}`));
    const pre = $("#errors"); pre.textContent = parts.join("\n\n"); pre.classList.toggle("hidden", !parts.length);
  }
  function renderSpec(d) {
    const pane = $("#paneSpec");
    if (!d.spec) return;
    pane.replaceChildren(h("h3", { text: d.spec.title }), h("p", { text: d.spec.summary }));
    const list = (title, items, code) => items && items.length && pane.append(h("h3", { text: title }),
      h("ul", {}, items.map((i) => h("li", {}, code ? h("code", { text: i.signature || i.name }) : i, code && i.description ? ` — ${i.description}` : ""))));
    list("Functional requirements", d.spec.functional_requirements);
    list("Constraints", d.spec.constraints); list("Edge cases", d.spec.edge_cases); list("Public API", d.spec.public_api, true);
    if (d.plan) pane.append(h("h3", { text: "Implementation plan" }), h("ol", {}, d.plan.map((s) => h("li", {}, h("b", { text: s.title }), s.details ? `: ${s.details}` : ""))));
  }
  function render(d) {
    lastData = d;
    const finished = TERMINAL.has(d.status);
    const sb = $("#statusBadge"); sb.className = `pill ${d.status === "queued" ? "running" : d.status}`;
    sb.textContent = { queued: "Queued", running: "Running", passed: "Passed", failed: "Limit reached", error: "Error", cancelled: "Cancelled" }[d.status];
    renderStages(d.stages, finished); renderLogs(d.logs); renderTests(d); renderSpec(d);
    if (d.code && (!dirty || finished && taskId !== renderedTask)) { setCode(codeEd, d.code); dirty = false; }
    if (d.tests) setCode(testsEd, d.tests);
    renderedTask = taskId;
    const bar = $("#approveBar");
    bar.classList.toggle("hidden", !(finished && d.code));
    if (finished) {
      $("#approveMsg").textContent = d.approved ? "✔ Approved and saved. Nothing was deployed or pushed."
        : d.status === "passed" ? "All tests passed. Review or edit the code, then approve it."
        : d.status === "error" ? `The agent stopped: ${d.error}` : "Tests are still failing after the iteration limit. Review the latest code and errors, or edit and re-run.";
      $("#approveBtn").disabled = !!d.approved;
    }
    updateButtons();
  }
  let renderedTask = null;
  function updateButtons() {
    const finished = lastData && TERMINAL.has(lastData.status);
    const running = lastData && !finished;
    $("#startBtn").disabled = !!running; $("#runBtn").disabled = !(finished && lastData.code);
  }

  // ---------- history ----------
  async function refreshTasks() {
    try {
      const tasks = await api("/api/agent/tasks");
      const ul = $("#taskList"); ul.replaceChildren();
      if (!tasks.length) ul.append(h("li", { class: "muted", text: "No tasks yet." }));
      tasks.forEach((t) => {
        const li = h("li", { class: `task-item${t.task_id === taskId ? " active" : ""}`, tabindex: "0" },
          h("b", { text: t.title }), h("small", { text: `${t.status}${t.approved ? " · approved" : ""} · ${t.iterations} run(s)` }));
        li.addEventListener("click", () => selectTask(t.task_id));
        ul.append(li);
      });
    } catch (e) { /* ignore */ }
  }
  async function refreshIterations() {
    const box = $("#iterations");
    if (!taskId) { box.replaceChildren(h("p", { class: "muted", text: "Select a task to inspect its iterations." })); return; }
    try {
      const hist = await api(`/api/agent/history/${taskId}`);
      box.replaceChildren(h("p", { class: "muted", text: hist.task }));
      hist.iterations.forEach((it) => {
        const label = it.kind === "initial" ? "Initial attempt" : it.kind === "debug" ? `Debug fix ${it.iteration}` : "Manual run";
        const r = it.test_report;
        const det = h("details", { class: "iter" }, h("summary", {}, h("b", { text: label }),
          h("span", { class: `pill ${it.passed ? "passed" : "failed"}`, text: it.passed ? "passed" : "failing" }),
          h("span", { class: "muted", text: r ? `${r.passed_count}/${r.total} tests` : "" })));
        const body = h("div", { class: "body" });
        if (it.analysis) body.append(h("p", {}, h("b", { text: "Debugger analysis: " }), it.analysis));
        const ex = it.execution;
        if (ex && !ex.ok) body.append(h("pre", { text: ex.stderr || ex.blocked_reason || `exit ${ex.exit_code}` }));
        body.append(h("pre", { text: it.code }));
        const load = h("button", { class: "btn small", text: "Load into editor" });
        load.addEventListener("click", () => { setCode(codeEd, it.code); dirty = true; codeEd.refresh(); updateButtons(); window.scrollTo({ top: 0, behavior: "smooth" }); });
        body.append(load); det.append(body); box.append(det);
      });
    } catch (e) { /* ignore */ }
  }
  async function selectTask(id) {
    stopPolling(); taskId = id; dirty = false; logCount = 0;
    try { render(await api(`/api/agent/status/${id}`)); if (!TERMINAL.has(lastData.status)) startPolling(); } catch (e) { banner(e.message); }
    refreshTasks(); refreshIterations();
  }

  // ---------- polling / actions ----------
  function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
  function startPolling() {
    stopPolling();
    const tick = async () => {
      try {
        const d = await api(`/api/agent/status/${taskId}`); render(d);
        if (TERMINAL.has(d.status)) { stopPolling(); refreshTasks(); refreshIterations(); }
      } catch (e) { stopPolling(); banner(e.message); }
    };
    pollTimer = setInterval(tick, 1000); tick();
  }
  function clearOutputs() {
    stopPolling(); taskId = null; lastData = null; dirty = false; logCount = 0; renderedTask = null;
    setCode(codeEd, ""); setCode(testsEd, ""); $("#logs").replaceChildren(); $("#cases").replaceChildren();
    $("#paneSpec").replaceChildren(h("p", { class: "muted", text: "The structured specification and plan appear here." }));
    $("#approveBar").classList.add("hidden"); $("#errors").classList.add("hidden");
    $("#stages").replaceChildren(); $("#progressBar").style.width = "0";
    $("#statusBadge").className = "pill pending"; $("#statusBadge").textContent = "Idle";
    $("#testBadge").className = "pill pending"; $("#testBadge").textContent = "No run yet";
    ["Total", "Passed", "Failed", "Time"].forEach((n) => { $(`#stat${n}`).textContent = "–"; });
    banner(""); updateButtons(); initStages();
  }
  function initStages() {
    renderStages(["Requirement Analysis", "Planning", "Code Generation", "Code Execution", "Testing", "Debugging", "Final Review"]
      .map((label) => ({ id: label.toLowerCase(), label, status: "pending", detail: "" })), false);
  }
  async function start() {
    const task = $("#task").value.trim();
    if (task.length < 10) { banner("Describe the coding task in at least 10 characters."); return; }
    clearOutputs();
    $("#startBtn").disabled = true;
    try {
      const res = await post("/api/agent/run", { task, language: $("#language").value, max_iterations: Number($("#maxIter").value) });
      taskId = res.task_id; startPolling(); refreshTasks();
    } catch (e) { banner(e.message); updateButtons(); }
  }
  async function runEdited() {
    $("#runBtn").disabled = true; banner("Running edited code…", true);
    try {
      await post("/api/agent/execute", { task_id: taskId, code: codeEd.get() });
      dirty = false; banner("");
      render(await api(`/api/agent/status/${taskId}`)); refreshIterations(); refreshTasks();
    } catch (e) { banner(e.message); }
    updateButtons();
  }
  async function approve() {
    if (lastData && lastData.status !== "passed" && !confirm("Tests are not passing. Approve this code anyway?")) return;
    try {
      const res = await post("/api/agent/approve", { task_id: taskId, code: codeEd.get(), git_commit: $("#gitCommit").checked });
      dirty = false; banner(`Approved. Saved to ${res.saved_to}${res.commit_info ? " — " + res.commit_info.reason : ""}`, true);
      render(await api(`/api/agent/status/${taskId}`)); refreshTasks();
    } catch (e) { banner(e.message); }
  }
  async function reset() {
    try { await post("/api/agent/reset", { task_id: taskId }); } catch (e) { /* task may be gone */ }
    clearOutputs(); refreshTasks(); refreshIterations();
  }
  function download() {
    const code = codeEd.get(); if (!code) return;
    const blob = new Blob([code], { type: "text/x-python" });
    const a = h("a", { href: URL.createObjectURL(blob), download: "solution.py" });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  async function copy() {
    const code = codeEd.get(); if (!code) return;
    try { await navigator.clipboard.writeText(code); } catch (_) {
      const ta = h("textarea", {}); ta.value = code; document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
    }
    const b = $("#copyBtn"); b.textContent = "Copied!"; setTimeout(() => { b.textContent = "Copy"; }, 1200);
  }

  // ---------- wiring ----------
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    ["code", "tests", "spec"].forEach((n) => $(`#pane${n[0].toUpperCase() + n.slice(1)}`).classList.toggle("hidden", n !== tab.dataset.tab));
    codeEd.refresh(); testsEd.refresh();
  }));
  SAMPLES.forEach(([name, text]) => {
    const chip = h("button", { class: "chip", type: "button", text: name });
    chip.addEventListener("click", () => { $("#task").value = text; $("#task").focus(); });
    $("#samples").append(chip);
  });
  $("#startBtn").addEventListener("click", start);
  $("#clearBtn").addEventListener("click", () => { $("#task").value = ""; $("#task").focus(); });
  $("#resetBtn").addEventListener("click", reset);
  $("#runBtn").addEventListener("click", runEdited);
  $("#approveBtn").addEventListener("click", approve);
  $("#downloadBtn").addEventListener("click", download);
  $("#copyBtn").addEventListener("click", copy);
  $("#task").addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") start(); });

  (async function init() {
    initStages();
    try {
      const health = await api("/api/health");
      $("#modelBadge").textContent = `model: ${health.model}`; $("#limitBadge").textContent = `timeout: ${health.execution_timeout}s`;
      $("#maxIter").value = String(health.default_max_iterations);
      if (!health.gemini_configured) banner("GEMINI_API_KEY is not configured. Copy .env.example to .env, add your key and restart the server.");
    } catch (e) { banner("Cannot reach the backend: " + e.message); }
    refreshTasks();
  })();
})();
