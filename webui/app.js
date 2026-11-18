// Poke-Controller web console — prototype behaviour (mock telemetry, no backend)
// Structure: status strip / stage / context sidebar / log dock / floating pad.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const padMode = new URLSearchParams(location.search).has("pad");
const bc = "BroadcastChannel" in window ? new BroadcastChannel("pokecon-pad") : null;

const PROFILES = [
  { id: "default", label: "default", color: "#58a6ff" },
  { id: "raids", label: "raids", color: "#f47067" },
  { id: "breeding", label: "breeding", color: "#4cc38a" },
];

const COMMANDS = [
  { name: "色違い厳選（自動孵化）", tag: "厳選", kind: "Python", used: 412, last: "2 分前" },
  { name: "一括孵化（30 個）", tag: "厳選", kind: "Python", used: 208, last: "昨日" },
  { name: "対戦用個体チェック", tag: "対戦", kind: "Python", used: 96, last: "3 日前" },
  { name: "レイド周回（星 5）", tag: "対戦", kind: "Python", used: 154, last: "1 時間前" },
  { name: "ジャイロ操作テスト", tag: "テスト", kind: "Python", used: 31, last: "先週" },
  { name: "画像認識テスト", tag: "テスト", kind: "Python", used: 74, last: "4 時間前" },
  { name: "メニュー巡回", tag: "ユーティリティ", kind: "Python", used: 22, last: "先月" },
  { name: "ボックス整理", tag: "育成", kind: "Python", used: 18, last: "先月" },
  { name: "通信進化", tag: "育成", kind: "Python", used: 9, last: "2 か月前" },
  { name: "連打（A・10Hz）", tag: "テスト", kind: "MCU", used: 63, last: "今日" },
  { name: "スティック円描き", tag: "テスト", kind: "MCU", used: 12, last: "先週" },
  { name: "ボタン送信テスト", tag: "テスト", kind: "MCU", used: 5, last: "先月" },
];

const TAGS = ["すべて", "厳選", "対戦", "育成", "テスト", "ユーティリティ"];

const HISTORY = [
  { t: "14:52:07", name: "色違い厳選（自動孵化）", res: "完了", dur: "18m 42s", cls: "ok" },
  { t: "14:20:11", name: "画像認識テスト", res: "完了", dur: "0m 48s", cls: "ok" },
  { t: "13:58:02", name: "レイド周回（星 5）", res: "停止", dur: "4m 05s", cls: "stop" },
  { t: "11:41:30", name: "連打（A・10Hz）", res: "エラー", dur: "0m 12s", cls: "err" },
  { t: "10:02:55", name: "一括孵化（30 個）", res: "完了", dur: "26m 10s", cls: "ok" },
];

const state = {
  connected: true,
  phase: "ready",
  transport: "pico_uart",
  port: "COM4",
  baud: 115200,
  profile: "default",
  view: "run",
  tab: "log",
  follow: true,
  problemOnly: false,
  levels: new Set(["info", "result", "warn", "error", "debug"]),
  search: "",
  cmdQuery: "",
  tag: "すべて",
  selected: null,
  logs: [],
  inputs: [],
  wires: [],
  run: { status: "idle", name: null, started: 0, paused: false, step: 0, total: 0, inputs: 0, timer: null, ticker: null },
  frozen: false,
  crop: false,
  newCount: 0,
  deadzone: 2048,
  volume: 60,
  audio: 0.42,
  gamepad: null,
};

const feed = $("#feed");
const frame = $("#frame");
const ctx = feed.getContext("2d");
const pad = $("#pad");
const pressed = new Set();
const stickValues = { L: { x: 0, y: 0 }, R: { x: 0, y: 0 } };
let lastStickLog = 0;

/* ---------------- helpers ---------------- */

const hhmmss = (d = new Date()) =>
  `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
const ms3 = (d = new Date()) => String(d.getMilliseconds()).padStart(3, "0");
const stamp = () => `${hhmmss()}.${ms3()}`;
const elapsed = (from) => {
  const s = Math.max(0, Math.floor((Date.now() - from) / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
};

function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), 2600);
}

function downloadCanvas(canvas, name) {
  canvas.toBlob((blob) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1500);
  });
}

/* ---------------- log ---------------- */

const LEVEL_LABEL = { info: "INFO", result: "RESULT", warn: "WARN", error: "ERROR", debug: "DEBUG" };

function addLog(level, msg, opts = {}) {
  const line = { id: ++addLog.id, t: stamp(), level, msg, kind: opts.kind || "log" };
  if (opts.kind === "input") state.inputs.push(line);
  else if (opts.kind === "wire") state.wires.push(line);
  else state.logs.push(line);

  const arr = opts.kind === "input" ? state.inputs : opts.kind === "wire" ? state.wires : state.logs;
  if (arr.length > 600) arr.splice(0, arr.length - 600);

  if (line.kind === "log") updateProblemCount();
  updateCounts();
  if ((state.tab === "log" && line.kind === "log") || (state.tab === "input" && line.kind === "input") || (state.tab === "wire" && line.kind === "wire")) {
    renderLog(line.id);
  }
  if (line.kind === "wire") renderMiniWire();
  return line;
}

function updateCounts() {
  $("#countLog").textContent = state.logs.length;
  $("#countInput").textContent = state.inputs.length;
  $("#countWire").textContent = state.wires.length;
}

function updateProblemCount() {
  const n = state.logs.filter((l) => l.level === "warn" || l.level === "error").length;
  $("#problemCount").textContent = n;
  $("#problemBtn").classList.toggle("has-problem", n > 0);
}

function visibleLines() {
  const arr = state.tab === "log" ? state.logs : state.tab === "input" ? state.inputs : state.wires;
  let out = arr;
  if (state.tab === "log") out = out.filter((l) => state.levels.has(l.level));
  if (state.search) {
    const q = state.search.toLowerCase();
    out = out.filter((l) => l.msg.toLowerCase().includes(q));
  }
  return out.slice(-500);
}

function highlight(msg) {
  const safe = esc(msg);
  if (!state.search) return safe;
  const q = esc(state.search).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return safe.replace(new RegExp(`(${q})`, "gi"), '<span class="hit">$1</span>');
}

function renderLog(freshId) {
  const lines = visibleLines();
  const host = $("#logLines");
  if (!lines.length) {
    host.innerHTML = `<div class="log-empty">${state.search ? "一致する行がありません。" : "まだログがありません。コマンドを実行すると流れます。"}</div>`;
    return;
  }
  host.innerHTML = lines
    .map((l) => {
      const lv = l.kind === "log" ? LEVEL_LABEL[l.level] || l.level.toUpperCase() : l.kind === "input" ? "INPUT" : "WIRE";
      return `<div class="log-line ${l.level}${l.id === freshId ? " fresh" : ""}" data-id="${l.id}"><span class="lt">${l.t}</span><span class="lv">${lv}</span><span class="msg">${highlight(l.msg)}</span></div>`;
    })
    .join("");

  const sc = $("#logScroll");
  if (state.follow) {
    sc.scrollTop = sc.scrollHeight;
    state.newCount = 0;
    $("#newLines").hidden = true;
  } else {
    state.newCount += freshId ? 1 : 0;
    const btn = $("#newLines");
    btn.hidden = false;
    btn.textContent = `↓ 新着 ${state.newCount} 行`;
  }
}

function renderMiniWire() {
  const host = $("#miniWire");
  if (!host) return;
  host.textContent = state.wires.slice(-9).map((l) => `${l.t}  ${l.msg}`).join("\n");
}

/* ---------------- run lifecycle ---------------- */

const RUN_LINES = [
  ["info", "画像認識: バトルメニューを待機"],
  ["info", "テンプレート一致 0.94 → 「たたかう」"],
  ["result", "たたかう を選択"],
  ["info", "ウェイト 1.2s（アニメーション待ち）"],
  ["debug", "frame 1280x720 / diff 0.02"],
  ["info", "テンプレート一致 0.91 → 「ポケモン」"],
  ["result", "ポケモン を選択"],
  ["info", "ボタン A を 80ms 押下"],
  ["result", "HP バー検出: 相手 62% / 自分 88%"],
  ["debug", "送信キュー depth=0"],
  ["info", "ウェイト 0.8s"],
  ["result", "「いれかえ」を選択"],
];

function startRun(cmd) {
  if (!state.connected) {
    toast("シリアルが未接続です。機器タブで接続してください。", "err");
    addLog("error", "実行できません: transport が ready ではありません");
    return;
  }
  if (state.run.status === "running") {
    toast("すでに実行中です。", "err");
    return;
  }
  if (!cmd) {
    toast("コマンドを選んでください。", "err");
    return;
  }
  const r = state.run;
  r.status = "running";
  r.name = cmd.name;
  r.started = Date.now();
  r.paused = false;
  r.step = 0;
  r.total = 40 + Math.floor(Math.random() * 40);
  r.inputs = 0;
  state.selected = cmd.name;

  addLog("info", `command_start name=${cmd.name} kind=${cmd.kind}`);
  addLog("info", `transport=${state.transport} port=${state.port} baud=${state.baud} arb=human`);
  $("#runName").textContent = cmd.name;
  $("#runPill").textContent = "実行中";
  $("#runPill").className = "state-pill running";
  $("#btnRun").disabled = true;
  $("#runActions").hidden = false;
  $("#runText").textContent = "実行中";
  updateRunUI();

  clearInterval(r.timer);
  clearInterval(r.ticker);
  r.timer = setInterval(() => {
    if (r.paused) return;
    r.step = Math.min(r.total, r.step + 1);
    const [lv, msg] = RUN_LINES[r.step % RUN_LINES.length];
    addLog(lv, msg);
    if (r.step % 3 === 0) addLog("wire", `TX  S 00000000000000${(r.step % 16).toString(16)}`, { kind: "wire" });
    if (r.step % 7 === 0) addLog("wire", "RX  O 0000 → 0x2a 0x1f 0x00 0x03", { kind: "wire" });
    if (r.step > 8 && Math.random() < 0.06) addLog("warn", "画像一致が不安定です（0.72）— しきい値 0.80");
    updateRunUI();
    if (r.step >= r.total) finishRun("完了");
  }, 620);

  r.ticker = setInterval(() => {
    $("#runTime").textContent = r.paused ? "一時停止" : elapsed(r.started);
  }, 250);

  toast(`実行: ${cmd.name}`);
}

function pauseRun() {
  const r = state.run;
  if (r.status !== "running") return;
  r.paused = !r.paused;
  $("#runPill").textContent = r.paused ? "一時停止" : "実行中";
  $("#runPill").className = `state-pill ${r.paused ? "" : "running"}`;
  $("#runText").textContent = r.paused ? "一時停止" : "実行中";
  $("#runTime").textContent = r.paused ? "一時停止" : elapsed(r.started);
  addLog("info", r.paused ? "一時停止しました" : "再開しました");
}

function stopRun(reason = "停止") {
  const r = state.run;
  if (r.status === "idle") return;
  clearInterval(r.timer);
  clearInterval(r.ticker);
  const dur = elapsed(r.started);
  addLog("warn", `command_stop reason=${reason} step=${r.step}/${r.total} elapsed=${dur}`);
  addHistory(r.name, reason === "完了" ? "完了" : "停止", dur, reason === "完了" ? "ok" : "stop");
  r.status = "idle";
  r.paused = false;
  $("#runActions").hidden = true;
  $("#runText").textContent = "待機";
  $("#runTime").textContent = "";
  $("#runPill").textContent = "停止";
  $("#runPill").className = "state-pill";
  $("#btnRun").disabled = false;
  scheduleRunCardReset();
  toast(reason === "完了" ? "コマンドが完了しました" : "停止しました");
}

function finishRun(kind) {
  const r = state.run;
  if (r.status !== "running") return;
  clearInterval(r.timer);
  clearInterval(r.ticker);
  const dur = elapsed(r.started);
  addLog("result", `command_end result=${kind} elapsed=${dur} inputs=${r.inputs}`);
  addHistory(r.name, kind, dur, "ok");
  r.status = "idle";
  r.paused = false;
  $("#runActions").hidden = true;
  $("#runText").textContent = "待機";
  $("#runTime").textContent = "";
  $("#runPill").textContent = "完了";
  $("#runPill").className = "state-pill ok";
  $("#btnRun").disabled = false;
  $("#runProgress").style.width = "100%";
  toast(`${r.name} 完了`, "ok");
  scheduleRunCardReset();
}

function scheduleRunCardReset() {
  setTimeout(() => {
    if (state.run.status !== "idle") return;
    $("#runPill").textContent = "待機";
    $("#runPill").className = "state-pill";
    $("#runName").textContent = "なし";
    $("#runStep").textContent = "0 / 0";
    $("#runInputs").textContent = "0";
    $("#runProgress").style.width = "0%";
  }, 5000);
}

function updateRunUI() {
  const r = state.run;
  $("#runStep").textContent = `${r.step} / ${r.total}`;
  $("#runInputs").textContent = r.inputs;
  $("#runProgress").style.width = `${Math.round((r.step / r.total) * 100)}%`;
  $("#runTime").textContent = r.status === "running" ? (r.paused ? "一時停止" : elapsed(r.started)) : "";
}

function addHistory(name, res, dur, cls) {
  HISTORY.unshift({ t: hhmmss(), name, res, dur, cls });
  if (HISTORY.length > 60) HISTORY.pop();
  renderHistory();
}

/* ---------------- input ---------------- */

const ALL_BUTTONS = ["A", "B", "X", "Y", "UP", "DOWN", "LEFT", "RIGHT", "L", "R", "ZL", "ZR", "PLUS", "MINUS", "HOME", "CAPTURE"];

function press(name, source = "mouse", fromRemote = false) {
  if (!name || pressed.has(name)) return;
  pressed.add(name);
  reflect(name, true);
  state.run.inputs++;
  addLog("debug", `PRESS ${name}`, { kind: "input" });
  addLog("wire", `TX  ${name} down`, { kind: "wire" });
  if (state.run.status === "running") updateRunUI();
  if (source === "mouse") vibrate(name);
  if (!fromRemote && padMode) sendInput({ type: "btn", name, down: true, source });
}

function release(name, source = "mouse", fromRemote = false) {
  if (!pressed.has(name)) return;
  pressed.delete(name);
  reflect(name, false);
  addLog("debug", `RELEASE ${name}`, { kind: "input" });
  if (!fromRemote && padMode) sendInput({ type: "btn", name, down: false, source });
}

function sendInput(msg) {
  if (bc) bc.postMessage(msg);
  if (window.opener && !window.opener.closed) {
    try {
      window.opener.postMessage(msg, location.origin);
    } catch {
      /* opener gone */
    }
  }
}

function reflect(name, on) {
  $$(`[data-btn="${name}"]`).forEach((b) => b.classList.toggle("is-down", on));
  const mb = $(`.mb[data-b="${name}"]`);
  if (mb) mb.classList.toggle("on", on);
  renderOverlay();
}

function renderOverlay() {
  const host = $("#inputOverlay");
  if (!host) return;
  host.innerHTML = [...pressed].slice(0, 6).map((n) => `<span class="badge in">${n}</span>`).join("");
}

function vibrate(name) {
  const el = document.querySelector(`.pbtn[data-btn="${name}"]`);
  if (el && el.animate) el.animate([{ transform: "scale(1)" }, { transform: "scale(0.94)" }, { transform: "scale(1)" }], { duration: 120 });
}

function setupPad() {
  $$(".pbtn").forEach((btn) => {
    const name = btn.dataset.btn;
    btn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      btn.setPointerCapture(e.pointerId);
      press(name, "mouse");
    });
    const up = (e) => {
      e.preventDefault();
      release(name, "mouse");
    };
    btn.addEventListener("pointerup", up);
    btn.addEventListener("pointercancel", up);
    btn.addEventListener("lostpointercapture", up);
    btn.addEventListener("contextmenu", (e) => e.preventDefault());
    btn.addEventListener("click", (e) => {
      if (e.detail !== 0) return; // pointer already handled by pointerdown/up
      press(name, "keyboard");
      setTimeout(() => release(name, "keyboard"), 140);
    });
  });

  $$(".stick").forEach((el) => {
    const which = el.dataset.stick;
    let active = false;
    const knob = $(".stick-knob", el);
    const apply = (vx, vy) => {
      const max = el.clientWidth / 2 - 8;
      knob.style.transform = `translate(${(vx / 100) * max}px, ${(-vy / 100) * max}px)`;
      stickValues[which] = { x: vx, y: vy };
      el.setAttribute("aria-valuenow", String(vx));
      el.setAttribute("aria-valuetext", `${vx}, ${vy}`);
      $(`#mirror${which}`).textContent = `${vx}, ${vy}`;
      if (Date.now() - lastStickLog > 140 && (Math.abs(vx) > 8 || Math.abs(vy) > 8)) {
        lastStickLog = Date.now();
        addLog("debug", `${which}STICK (${vx}, ${vy})`, { kind: "input" });
        addLog("wire", `TX  ${which} ${vx},${vy}`, { kind: "wire" });
      }
    };
    const move = (e) => {
      const r = el.getBoundingClientRect();
      let dx = e.clientX - (r.left + r.width / 2);
      let dy = e.clientY - (r.top + r.height / 2);
      const max = r.width / 2 - 8;
      const len = Math.hypot(dx, dy);
      if (len > max) {
        dx = (dx / len) * max;
        dy = (dy / len) * max;
      }
      apply(Math.round((dx / max) * 100), Math.round((-dy / max) * 100));
    };
    const end = () => {
      if (!active) return;
      active = false;
      el.classList.remove("is-down");
      apply(0, 0);
      addLog("debug", `${which}STICK (0, 0)`, { kind: "input" });
    };
    el.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      active = true;
      el.setPointerCapture(e.pointerId);
      el.classList.add("is-down");
      move(e);
    });
    el.addEventListener("pointermove", (e) => {
      if (active) move(e);
    });
    el.addEventListener("pointerup", (e) => {
      e.preventDefault();
      end();
    });
    el.addEventListener("pointercancel", end);
    el.addEventListener("lostpointercapture", end);
    el.addEventListener("keydown", (e) => {
      const step = e.shiftKey ? 10 : 25;
      const v = stickValues[which];
      let x = v.x;
      let y = v.y;
      if (e.key === "ArrowLeft") x -= step;
      else if (e.key === "ArrowRight") x += step;
      else if (e.key === "ArrowUp") y += step;
      else if (e.key === "ArrowDown") y -= step;
      else return;
      e.preventDefault();
      apply(Math.max(-100, Math.min(100, x)), Math.max(-100, Math.min(100, y)));
    });
    el.addEventListener("keyup", (e) => {
      if (e.key.startsWith("Arrow")) apply(0, 0);
    });
  });

  // keyboard
  const KEYMAP = {
    ArrowUp: "UP",
    ArrowDown: "DOWN",
    ArrowLeft: "LEFT",
    ArrowRight: "RIGHT",
    KeyZ: "B",
    KeyX: "A",
    KeyA: "Y",
    KeyS: "X",
    KeyQ: "L",
    KeyE: "R",
    Digit1: "ZL",
    Digit3: "ZR",
    Enter: "PLUS",
    Backspace: "MINUS",
    KeyH: "HOME",
    KeyC: "CAPTURE",
  };
  const typing = (el) => el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName);
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (typing(document.activeElement) || document.activeElement?.classList?.contains("stick") || document.querySelector("dialog[open]")) return;
    const name = KEYMAP[e.code];
    if (!name) return;
    if (e.code === "Backspace" || e.code.startsWith("Arrow")) e.preventDefault();
    if (!e.repeat) press(name, "keyboard");
  });
  document.addEventListener("keyup", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (document.activeElement?.classList?.contains("stick")) return;
    const name = KEYMAP[e.code];
    if (!name) return;
    release(name, "keyboard");
  });
}

function pollGamepad() {
  const pads = navigator.getGamepads ? navigator.getGamepads() : [];
  const gp = pads && [...pads].find(Boolean);
  if (gp && $("#swGamepad")?.checked) {
    if (state.gamepad !== gp.index) {
      state.gamepad = gp.index;
      $("#padSource").textContent = "ゲームパッド";
      addLog("info", `gamepad_connected ${gp.id}`);
    }
    const MAP = { 0: "B", 1: "A", 2: "Y", 3: "X", 4: "L", 5: "R", 6: "ZL", 7: "ZR", 8: "MINUS", 9: "PLUS", 12: "UP", 13: "DOWN", 14: "LEFT", 15: "RIGHT", 16: "HOME" };
    for (const [i, name] of Object.entries(MAP)) {
      const b = gp.buttons[i];
      if (!b) continue;
      if (b.pressed) press(name, "gamepad", true);
      else release(name, "gamepad", true);
    }
  } else if (state.gamepad !== null) {
    state.gamepad = null;
    $("#padSource").textContent = "マウス";
  }
  requestAnimationFrame(pollGamepad);
}

/* ---------------- capture feed (mock) ---------------- */

let frames = 0;
let lastFpsAt = performance.now();
let latency = 18;

function drawFeed(t) {
  if (state.frozen) {
    requestAnimationFrame(drawFeed);
    return;
  }
  const w = feed.width;
  const h = feed.height;
  ctx.fillStyle = "#05080b";
  ctx.fillRect(0, 0, w, h);

  // floor grid
  ctx.strokeStyle = "rgba(88,166,255,0.07)";
  ctx.lineWidth = 1;
  for (let x = 0; x <= w; x += 80) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();
  }
  for (let y = 0; y <= h; y += 80) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }

  // arena
  ctx.strokeStyle = "rgba(88,166,255,0.28)";
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.ellipse(w / 2, h / 2 + 40, 330, 170, 0, 0, Math.PI * 2);
  ctx.stroke();

  // opponents
  const opp = [
    { x: w / 2 - 210, y: h / 2 + 30, c: "#f47067" },
    { x: w / 2 + 210, y: h / 2 + 60, c: "#e2a33c" },
  ];
  for (const o of opp) {
    ctx.fillStyle = o.c;
    ctx.globalAlpha = 0.85;
    ctx.beginPath();
    ctx.arc(o.x, o.y, 26, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.fillStyle = "#05080b";
    ctx.fillRect(o.x - 18, o.y - 6, 36, 12);
  }

  // player
  const px = w / 2 + Math.sin(t / 1400) * 200;
  const py = h / 2 + 40 + Math.cos(t / 1900) * 90;
  ctx.strokeStyle = "#58a6ff";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(px, py, 34, 0, Math.PI * 2);
  ctx.stroke();
  ctx.fillStyle = "#58a6ff";
  ctx.beginPath();
  ctx.arc(px, py, 22, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#06121f";
  ctx.font = "700 18px ui-monospace, monospace";
  ctx.textAlign = "center";
  ctx.fillText("P1", px, py + 6);

  // hud inside the feed (skip on small previews where the DOM HUD would collide)
  if (feed.clientWidth < 480) {
    frames++;
    const now0 = performance.now();
    if (now0 - lastFpsAt > 500) {
      lastFpsAt = now0;
      frames = 0;
      latency = 16 + Math.random() * 8;
    }
    requestAnimationFrame(drawFeed);
    return;
  }
  ctx.textAlign = "left";
  ctx.fillStyle = "rgba(229,237,244,0.9)";
  ctx.font = "600 22px ui-monospace, monospace";
  ctx.fillText("BATTLE 03", 40, 126);
  ctx.fillStyle = "rgba(147,163,178,0.9)";
  ctx.font = "500 16px ui-monospace, monospace";
  ctx.fillText("TURN " + (12 + Math.floor(t / 9000) % 9), 40, 152);
  ctx.textAlign = "right";
  ctx.fillStyle = "rgba(229,237,244,0.85)";
  ctx.font = "600 22px ui-monospace, monospace";
  ctx.fillText("03:" + String(10 + Math.floor(t / 1000) % 50).padStart(2, "0"), w - 40, 126);

  // hp bars
  const bar = (x, y, bw, val, color, label) => {
    ctx.fillStyle = "rgba(255,255,255,0.08)";
    ctx.fillRect(x, y, bw, 10);
    ctx.fillStyle = color;
    ctx.fillRect(x, y, bw * val, 10);
    ctx.fillStyle = "rgba(147,163,178,0.9)";
    ctx.font = "500 14px ui-monospace, monospace";
    ctx.textAlign = "left";
    ctx.fillText(label, x, y - 8);
  };
  bar(40, 196, 300, 0.88, "#4cc38a", "P1 HP");
  bar(w - 700, 196, 300, 0.62, "#f47067", "OPP HP");

  frames++;
  const now = performance.now();
  if (now - lastFpsAt > 500) {
    const fps = Math.min(60, Math.round((frames * 1000) / (now - lastFpsAt)));
    $("#hudFps").textContent = `${fps} fps`;
    $("#capFps").textContent = `${fps} fps · ${Math.round(latency)} ms`;
    frames = 0;
    lastFpsAt = now;
    latency = 16 + Math.random() * 8;
  }
  requestAnimationFrame(drawFeed);
}

/* ---------------- crop ---------------- */

function setupCrop() {
  const rect = $("#cropRect");
  let start = null;
  const local = (e) => {
    const b = frame.getBoundingClientRect();
    return { x: e.clientX - b.left, y: e.clientY - b.top, w: b.width, h: b.height };
  };
  feed.addEventListener("pointerdown", (e) => {
    if (!state.crop) return;
    e.preventDefault();
    feed.setPointerCapture(e.pointerId);
    const p = local(e);
    start = { x: Math.min(Math.max(p.x, 0), p.w), y: Math.min(Math.max(p.y, 0), p.h) };
    rect.hidden = false;
    rect.style.left = `${start.x}px`;
    rect.style.top = `${start.y}px`;
    rect.style.width = "0px";
    rect.style.height = "0px";
  });
  feed.addEventListener("pointermove", (e) => {
    if (!state.crop || !start) return;
    const p = local(e);
    const x = Math.min(Math.max(p.x, 0), p.w);
    const y = Math.min(Math.max(p.y, 0), p.h);
    rect.style.left = `${Math.min(x, start.x)}px`;
    rect.style.top = `${Math.min(y, start.y)}px`;
    rect.style.width = `${Math.abs(x - start.x)}px`;
    rect.style.height = `${Math.abs(y - start.y)}px`;
  });
  feed.addEventListener("pointerup", () => {
    if (!state.crop || !start) return;
    const b = frame.getBoundingClientRect();
    const left = parseFloat(rect.style.left);
    const top = parseFloat(rect.style.top);
    const cw = parseFloat(rect.style.width);
    const ch = parseFloat(rect.style.height);
    start = null;
    rect.hidden = true;
    const scale = feed.width / b.width;
    const sw = Math.round(cw * scale);
    const sh = Math.round(ch * scale);
    if (sw < 8 || sh < 8) return;
    const out = document.createElement("canvas");
    out.width = sw;
    out.height = sh;
    out.getContext("2d").drawImage(feed, left * scale, top * scale, sw, sh, 0, 0, sw, sh);
    downloadCanvas(out, `crop-${Date.now()}.png`);
    addLog("result", `クロップを保存しました (${sw}×${sh})`);
    toast(`切り抜き ${sw}×${sh} を保存`, "ok");
    setCrop(false);
  });
}

function setCrop(on) {
  state.crop = on;
  $("#btnCrop").classList.toggle("is-on", on);
  frame.style.cursor = on ? "crosshair" : "default";
  if (on) toast("映像の上をドラッグして範囲を選んでください");
}

/* ---------------- sidebar ---------------- */

function setView(view, syncHash = true) {
  state.view = view;
  if (syncHash) history.replaceState(null, "", `#${view}`);
  $$(".act[data-view]").forEach((b) => b.classList.toggle("is-active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("is-active", v.dataset.view === view));
  if (window.matchMedia("(max-width: 1040px)").matches) document.body.classList.add("sidebar-open");
}

function renderCommands() {
  const q = state.cmdQuery.toLowerCase();
  const rows = COMMANDS.filter(
    (c) => (state.tag === "すべて" || c.tag === state.tag) && (!q || c.name.toLowerCase().includes(q) || c.tag.includes(q)),
  );
  $("#cmdCount").textContent = `${rows.length} 件`;
  const host = $("#cmdList");
  if (!rows.length) {
    host.innerHTML = `<p class="log-empty">「${esc(state.cmdQuery)}」に一致するコマンドがありません。</p>`;
    return;
  }
  host.innerHTML = rows
    .map(
      (c) => `<button class="cmd-row${c.name === state.selected ? " is-selected" : ""}" data-name="${esc(c.name)}" role="option" aria-selected="${c.name === state.selected}">
        <span class="cmd-name">${esc(c.name)}</span>
        <span class="cmd-meta"><span class="tag">${esc(c.tag)}</span><span class="tnum">${c.used}</span><svg class="ic go"><use href="#i-play"/></svg></span>
      </button>`,
    )
    .join("");
  $$(".cmd-row", host).forEach((row) => {
    row.addEventListener("click", () => {
      state.selected = row.dataset.name;
      renderCommands();
    });
    row.addEventListener("dblclick", () => startRun(COMMANDS.find((c) => c.name === row.dataset.name)));
  });
}

function renderHistory() {
  const q = $("#histSearch")?.value.toLowerCase() || "";
  const rows = HISTORY.filter((h) => !q || h.name.toLowerCase().includes(q) || h.res.includes(q));
  $("#histCount").textContent = `${rows.length} 件`;
  $("#histList").innerHTML = rows
    .map(
      (h) => `<button class="hist-row${state.run.status === "running" && state.run.name === h.name && h.res === "実行中" ? " is-running" : ""}" data-name="${esc(h.name)}">
        <span class="hist-time">${h.t}</span>
        <span class="hist-name">${esc(h.name)}</span>
        <span class="hist-res ${h.cls}">${esc(h.res)}</span>
      </button>`,
    )
    .join("");
  $$(".hist-row", $("#histList")).forEach((row) => {
    row.addEventListener("dblclick", () => {
      const cmd = COMMANDS.find((c) => c.name === row.dataset.name);
      if (cmd) {
        setView("run");
        startRun(cmd);
      } else toast("このコマンドは一覧に見つかりません", "err");
    });
  });
}

function renderTags() {
  $("#tagChips").innerHTML = TAGS.map(
    (t) => `<button class="chip-tag${t === state.tag ? " is-on" : ""}" data-tag="${t}">${t}</button>`,
  ).join("");
  $$("#tagChips .chip-tag").forEach((b) =>
    b.addEventListener("click", () => {
      state.tag = b.dataset.tag;
      renderTags();
      renderCommands();
    }),
  );
}

function renderMirror() {
  $("#mirrorBtns").innerHTML = ALL_BUTTONS.map((b) => `<span class="mb" data-b="${b}">${b}</span>`).join("");
}

/* ---------------- palette ---------------- */

let palIndex = 0;
let palItems = [];

function paletteItems() {
  const items = COMMANDS.map((c) => ({ label: c.name, kind: "実行", run: () => startRun(c) }));
  items.push(
    { label: "実行パネルへ", kind: "画面", run: () => setView("run") },
    { label: "履歴へ", kind: "画面", run: () => setView("history") },
    { label: "機器設定へ", kind: "画面", run: () => setView("devices") },
    { label: "診断へ", kind: "画面", run: () => setView("diag") },
    { label: "ログを消去", kind: "操作", run: () => clearLogs() },
    { label: "追従の切り替え", kind: "操作", run: () => toggleFollow() },
    { label: "接続 / 切断", kind: "操作", run: () => toggleConnect() },
    { label: "スクリーンショット", kind: "操作", run: () => screenshot() },
  );
  return items;
}

function renderPalette() {
  const q = $("#paletteInput").value.toLowerCase();
  palItems = paletteItems().filter((i) => !q || i.label.toLowerCase().includes(q));
  palIndex = Math.min(palIndex, Math.max(0, palItems.length - 1));
  $("#paletteList").innerHTML = palItems.length
    ? palItems.map((i, n) => `<button class="pal-row${n === palIndex ? " is-active" : ""}" data-i="${n}"><span>${esc(i.label)}</span><span class="kind">${i.kind}</span></button>`).join("")
    : `<p class="log-empty">一致する項目がありません。</p>`;
  $$(".pal-row").forEach((b) =>
    b.addEventListener("click", () => {
      palItems[Number(b.dataset.i)].run();
      $("#palette").close();
    }),
  );
}

function openPalette() {
  $("#paletteInput").value = "";
  palIndex = 0;
  renderPalette();
  $("#palette").showModal();
  $("#paletteInput").focus();
}

/* ---------------- devices ---------------- */

function toggleConnect() {
  state.connected = !state.connected;
  state.phase = state.connected ? "ready" : "disconnected";
  $("#linkDot").className = `dot ${state.connected ? "ok" : ""}`;
  $("#linkText").textContent = state.transport;
  $("#linkSub").textContent = state.connected ? `${state.port} · ${state.baud}` : "未接続";
  $("#devPill").textContent = state.phase;
  $("#devPill").className = `state-pill ${state.connected ? "ok" : ""}`;
  $("#diagPill").textContent = state.phase;
  $("#diagPill").className = `state-pill ${state.connected ? "ok" : ""}`;
  $("#kvPhase").textContent = state.phase;
  $("#btnConnect").textContent = state.connected ? "切断" : "接続";
  $("#btnConnect").classList.toggle("primary", !state.connected);
  addLog(state.connected ? "info" : "warn", state.connected ? `serial_open ${state.port} @ ${state.baud} transport=${state.transport}` : "serial_close");
  toast(state.connected ? `${state.port} に接続しました` : "切断しました", state.connected ? "ok" : "");
}

function applyTransport() {
  const t = $("#selTransport").value;
  state.transport = t;
  $("#linkText").textContent = t;
  const descs = {
    legacy_text: "従来と同じテキスト行を pyserial で送る。ボーレートは固定。",
    pico_uart: "Pico UART（115200 固定）。200ms のキープアライブが必要。",
    "switch-bcon": "Switch-bcon へ STATE バイナリを送る（Pico 2 W・既定 1Mbps・HELLO 必須）。",
    "switch-bcon-proc": "Switch-bcon を別プロセスで送出（GUI 負荷を切り離す）。",
  };
  $("#transportDesc").textContent = descs[t];
  const isBcon = t.startsWith("switch-bcon");
  $("#bconCard").hidden = !isBcon;
  $("#selBaud").disabled = t === "legacy_text" || t === "pico_uart";
  const baud = isBcon ? "1000000" : t === "legacy_text" ? "9600" : "115200";
  $("#selBaud").value = baud;
  state.baud = Number(baud);
  $("#linkSub").textContent = state.connected ? `${state.port} · ${state.baud}` : "未接続";
  $("#kvBaud").textContent = String(state.baud);
  addLog("info", `transport_changed ${t}`);
}

/* ---------------- misc actions ---------------- */

function screenshot() {
  downloadCanvas(feed, `capture-${Date.now()}.png`);
  addLog("result", "キャプチャを保存しました (1920×1080)");
  toast("キャプチャを保存しました", "ok");
}

function clearLogs() {
  if (state.tab === "log") state.logs = [];
  else if (state.tab === "input") state.inputs = [];
  else state.wires = [];
  state.newCount = 0;
  $("#newLines").hidden = true;
  updateCounts();
  updateProblemCount();
  renderLog();
  renderMiniWire();
}

function toggleFollow() {
  state.follow = !state.follow;
  $("#followText").textContent = state.follow ? "ON" : "OFF";
  if (state.follow) renderLog();
  toast(`ログ追従 ${state.follow ? "ON" : "OFF"}`);
}

function openModal({ title, body, foot }) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = body;
  $("#modalFoot").innerHTML = foot || `<button class="btn" data-close>閉じる</button>`;
  $("#modal").showModal();
  $$("[data-close]", $("#modal")).forEach((b) => b.addEventListener("click", () => $("#modal").close()));
}

/* ---------------- boot ---------------- */

function init() {
  renderMirror();
  renderTags();
  renderCommands();
  renderHistory();
  updateCounts();

  // seed a previous session
  addLog("info", "Poke-Controller web console (mock) を開始しました");
  addLog("info", "profile=default transport=pico_uart port=COM4 baud=115200");
  addLog("result", "カメラ 1920×1080 / 60fps / MJPG で開きました");
  addLog("debug", "preview backend=sdl (D3D11)");
  addLog("warn", "音声デバイスの遅延を推定できません（--）");
  addLog("info", "コマンド 12 件を読み込みました（Python 9 / MCU 3）");
  addLog("result", "前回: 色違い厳選（自動孵化） 完了 18m 42s");
  addLog("info", "画像認識テンプレート 24 件を読み込みました");
  addLog("debug", "preview pump: frame-driven (no timer chain)");

  if (padMode) {
    document.body.classList.add("pad-mode");
    document.title = "コントローラ — Poke-Controller";
    setupPad();
    requestAnimationFrame(pollGamepad);
    return;
  }

  // stage
  const fitFeed = () => {
    const wrap = $("#videoWrap");
    const cs = getComputedStyle(wrap);
    const availW = wrap.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    const availH = wrap.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    const s = Math.min(availW / 16, availH / 9);
    frame.style.width = `${Math.floor(s * 16)}px`;
    frame.style.height = `${Math.floor(s * 9)}px`;
  };
  if (window.matchMedia("(max-width: 720px)").matches) {
    pad.classList.add("is-collapsed");
    $("#btnPadToggle").classList.remove("is-on");
  }
  new ResizeObserver(fitFeed).observe($("#videoWrap"));
  fitFeed();
  requestAnimationFrame(drawFeed);
  setupCrop();
  setupPad();
  requestAnimationFrame(pollGamepad);

  // sidebar
  $$(".act[data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  $("#cmdSearch").addEventListener("input", (e) => {
    state.cmdQuery = e.target.value;
    renderCommands();
  });
  $("#histSearch").addEventListener("input", renderHistory);
  $("#btnRun").addEventListener("click", () => startRun(COMMANDS.find((c) => c.name === state.selected)));
  $("#btnReload").addEventListener("click", () => {
    addLog("info", "コマンドを再読み込みしました（12 件）");
    toast("コマンドを再読み込みしました", "ok");
    renderCommands();
  });
  $("#btnPause").addEventListener("click", pauseRun);
  $("#btnStop").addEventListener("click", () => stopRun("停止"));

  // dock
  $$(".dtab").forEach((tab) =>
    tab.addEventListener("click", () => {
      state.tab = tab.dataset.log;
      $$(".dtab").forEach((t) => {
        t.classList.toggle("is-active", t === tab);
        t.setAttribute("aria-selected", String(t === tab));
      });
      $("#levelChips").style.visibility = state.tab === "log" ? "visible" : "hidden";
      state.newCount = 0;
      $("#newLines").hidden = true;
      renderLog();
    }),
  );
  $$("#levelChips .lvl").forEach((chip) =>
    chip.addEventListener("click", () => {
      const lv = chip.dataset.level;
      if (state.levels.has(lv)) state.levels.delete(lv);
      else state.levels.add(lv);
      chip.classList.toggle("is-on", state.levels.has(lv));
      renderLog();
    }),
  );
  $("#logSearch").addEventListener("input", (e) => {
    state.search = e.target.value;
    renderLog();
  });
  $("#logScroll").addEventListener("scroll", () => {
    const sc = $("#logScroll");
    const atBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 24;
    state.follow = atBottom;
    $("#followText").textContent = atBottom ? "ON" : "OFF";
    if (atBottom) {
      state.newCount = 0;
      $("#newLines").hidden = true;
    }
  });
  $("#newLines").addEventListener("click", () => {
    state.follow = true;
    $("#followText").textContent = "ON";
    $("#logScroll").scrollTop = $("#logScroll").scrollHeight;
    state.newCount = 0;
    $("#newLines").hidden = true;
  });
  $("#btnFollowDock").addEventListener("click", toggleFollow);
  $("#btnFollow").addEventListener("click", toggleFollow);
  $("#btnClear").addEventListener("click", clearLogs);
  $("#btnCopy").addEventListener("click", async () => {
    const text = visibleLines().map((l) => `${l.t} ${LEVEL_LABEL[l.level] || l.level} ${l.msg}`).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      toast("表示中のログをコピーしました", "ok");
    } catch {
      toast("コピーできませんでした", "err");
    }
  });

  // stage tools
  $("#btnShot").addEventListener("click", screenshot);
  $("#btnCrop").addEventListener("click", () => setCrop(!state.crop));
  $("#btnFreeze").addEventListener("click", () => {
    state.frozen = !state.frozen;
    $("#btnFreeze").classList.toggle("is-on", state.frozen);
    $("#btnFreeze").querySelector("span").textContent = state.frozen ? "再開" : "静止";
    $("#hudRes").textContent = state.frozen ? "PAUSED" : "1920×1080";
    toast(state.frozen ? "プレビューを止めました（取込は継続）" : "プレビューを再開しました");
  });
  const setPadOpen = (open) => {
    pad.classList.toggle("is-collapsed", !open);
    $("#btnPadToggle").classList.toggle("is-on", open);
  };
  $("#btnPadToggle").addEventListener("click", () => setPadOpen(pad.classList.contains("is-collapsed")));
  $("#padCollapse").addEventListener("click", () => setPadOpen(false));
  $("#padPopout").addEventListener("click", () => {
    const w = window.open(`${location.pathname}?pad=1`, "pokecon-pad", "width=340,height=340");
    if (!w) toast("ポップアップがブロックされました", "err");
  });
  $("#btnArbitration").addEventListener("click", () => {
    const order = ["human", "script", "off"];
    const next = order[(order.indexOf($("#arbText").textContent) + 1) % order.length];
    $("#arbText").textContent = next;
    $("#selArb").value = next;
    addLog("info", `arbitration_mode=${next}`);
  });
  $("#problemBtn").addEventListener("click", () => {
    state.problemOnly = !state.problemOnly;
    if (state.problemOnly) {
      state.levels = new Set(["warn", "error"]);
    } else {
      state.levels = new Set(["info", "result", "warn", "error", "debug"]);
    }
    $$("#levelChips .lvl").forEach((c) => c.classList.toggle("is-on", state.levels.has(c.dataset.level)));
    state.tab = "log";
    $$(".dtab").forEach((t) => t.classList.toggle("is-active", t.dataset.log === "log"));
    state.follow = false;
    renderLog();
    $("#logScroll").scrollTop = $("#logScroll").scrollHeight;
    toast(state.problemOnly ? "警告とエラーだけ表示" : "すべての水準を表示");
  });
  $("#btnSidebar").addEventListener("click", () => document.body.classList.toggle("sidebar-open"));
  $("#btnPalette").addEventListener("click", openPalette);
  $$("[data-goto]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.goto)));
  $("#btnEditor").addEventListener("click", () => {
    openModal({
      title: "Blockly エディタ",
      body: `<p>Blockly エディタは、このコンソールとは別の Web UI（ローカル HTTP の別タブ）として残します。保存するとコンソールのコマンド一覧が自動で再読み込みされる、という分担です。</p>
        <p class="desc">複雑な手順の組み立てはパッドや一覧ではなくブロックで行う、という判断です（NOOV の実装記録でも、複雑なコマンド作成にブラウザのパッドは向かないとされている）。このプロトタイプにはエディタ本体を含めていません。</p>`,
      foot: `<button class="btn" data-close>閉じる</button>`,
    });
  });

  // devices
  $("#selTransport").addEventListener("change", applyTransport);
  $("#btnConnect").addEventListener("click", toggleConnect);
  $("#selPort").addEventListener("change", (e) => {
    state.port = e.target.value.split(" ")[0];
    $("#linkSub").textContent = `${state.port} · ${state.baud}`;
  });
  $("#selBaud").addEventListener("change", (e) => {
    state.baud = Number(e.target.value);
    $("#linkSub").textContent = `${state.port} · ${state.baud}`;
    $("#kvBaud").textContent = String(state.baud);
  });
  $("#selArb").addEventListener("change", (e) => {
    $("#arbText").textContent = e.target.value;
    addLog("info", `arbitration_mode=${e.target.value}`);
  });
  $("#selFps").addEventListener("change", (e) => {
    addLog("info", `fps=${e.target.value}`);
    $("#capFps").textContent = `${e.target.value} fps · ${Math.round(latency)} ms`;
  });
  $("#selRes").addEventListener("change", (e) => {
    $("#capRes").textContent = e.target.value;
    $("#hudRes").textContent = e.target.value;
  });
  $("#rngDeadzone").addEventListener("input", (e) => {
    state.deadzone = Number(e.target.value);
    $("#deadzoneVal").textContent = e.target.value;
  });
  $("#rngVolume").addEventListener("input", (e) => {
    state.volume = Number(e.target.value);
    $("#volumeVal").textContent = `${state.volume}%`;
  });
  $("#swMonitor").addEventListener("change", (e) => {
    addLog("info", `monitor=${e.target.checked ? "on" : "off"}`);
  });
  $("#btnCameraReload").addEventListener("click", () => {
    addLog("info", "カメラを再列挙しました: 1 台");
    toast("カメラを再読み込みしました", "ok");
  });
  $("#btnFilter").addEventListener("click", () => {
    openModal({
      title: "表示フィルタ",
      body: `<div class="form">
        <label>ガンマ <input type="range" min="0.5" max="2.5" step="0.1" value="1.2"><span class="range-val">1.2</span></label>
        <label>コントラスト <input type="range" min="0" max="2" step="0.1" value="1.1"><span class="range-val">1.1</span></label>
        <label>輝度 <input type="range" min="-100" max="100" value="8"><span class="range-val">8</span></label>
        <label>彩度 <input type="range" min="0" max="2" step="0.1" value="1"><span class="range-val">1.0</span></label>
        <label>色抽出 <input type="checkbox"></label>
      </div><p class="desc">設定はプレビューに即時反映され、閉じても保持されます。</p>`,
      foot: `<button class="btn" data-close>キャンセル</button><button class="btn primary" data-close>適用</button>`,
    });
  });
  $$("[data-mock]").forEach((b) =>
    b.addEventListener("click", () => {
      addLog("info", b.dataset.mock);
      toast(b.dataset.mock);
    }),
  );
  $("#btnCopyReport").addEventListener("click", async () => {
    const report = ["Poke-Controller web console (prototype)", "OS: Windows 11 10.0.22631", "Python: 3.12.15", `profile: ${state.profile}`, `transport: ${state.transport}`, "traceback: (なし)"].join("\n");
    try {
      await navigator.clipboard.writeText(report);
      toast("エラー報告をコピーしました", "ok");
    } catch {
      toast("コピーできませんでした", "err");
    }
  });
  $("#btnHidHide").addEventListener("click", () => {
    openModal({
      title: "HidHide",
      body: `<dl class="kv"><div><dt>状態</dt><dd>未設定</dd></div><div><dt>検出</dt><dd>Pro Controller (HID)</dd></div><div><dt>クローク</dt><dd>OFF</dd></div></dl>
        <p class="desc">HidHide はゲームパッドを Windows の通常入力から隠し、このアプリだけが見えるようにします。適用後はゲームパッド操作が自動で有効になります。</p>`,
      foot: `<button class="btn" data-close>閉じる</button><button class="btn primary" data-close>適用</button>`,
    });
  });
  $("#btnKeymap").addEventListener("click", () => {
    const rows = [["A", "KeyX"], ["B", "KeyZ"], ["X", "KeyS"], ["Y", "KeyA"], ["L", "KeyQ"], ["R", "KeyE"], ["+", "Enter"], ["−", "Backspace"]];
    openModal({
      title: "キーコンフィグ",
      body: `<p class="desc">ボタンごとにキーボードの割り当てを変更します。重複は保存時に警告します。</p>
        <div class="form">${rows.map(([b, k]) => `<label>${b}<input value="${k}" aria-label="${b} のキー"></label>`).join("")}</div>`,
      foot: `<button class="btn" data-close>キャンセル</button><button class="btn primary" data-close>適用</button>`,
    });
  });
  $("#btnSerialMonitor").addEventListener("click", () => {
    setView("diag");
    document.body.classList.add("sidebar-open");
  });
  $("#btnWireTab").addEventListener("click", () => {
    state.tab = "wire";
    $$(".dtab").forEach((t) => t.classList.toggle("is-active", t.dataset.log === "wire"));
    renderLog();
  });

  // profile menu
  $("#profileBtn").addEventListener("click", () => {
    openModal({
      title: "プロファイル",
      body: `<p>プロファイルごとに <code>settings.&lt;name&gt;.ini</code> とロックファイルを持ち、色で見分けます。</p>
        <div class="form">${PROFILES.map(
          (p) => `<label class="switch-row"><span><span class="swatch" style="background:${p.color};display:inline-block;margin-right:6px"></span>${p.label}</span><button class="btn small" data-profile="${p.id}">${p.id === state.profile ? "使用中" : "切り替え"}</button></label>`,
        ).join("")}</div>`,
      foot: `<button class="btn" data-close>閉じる</button>`,
    });
    $$("[data-profile]", $("#modal")).forEach((b) =>
      b.addEventListener("click", () => {
        const p = PROFILES.find((x) => x.id === b.dataset.profile);
        state.profile = p.id;
        $("#profileName").textContent = p.label;
        $("#profileSwatch").style.background = p.color;
        addLog("info", `profile=${p.id}`);
        $("#modal").close();
        toast(`プロファイル: ${p.label}`, "ok");
      }),
    );
  });

  // palette
  $("#paletteInput").addEventListener("input", () => {
    palIndex = 0;
    renderPalette();
  });
  $("#paletteInput").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      palIndex = Math.min(palIndex + 1, palItems.length - 1);
      renderPalette();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      palIndex = Math.max(palIndex - 1, 0);
      renderPalette();
    } else if (e.key === "Enter") {
      e.preventDefault();
      palItems[palIndex]?.run();
      $("#palette").close();
    }
  });
  $("#modalClose").addEventListener("click", () => $("#modal").close());

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      openPalette();
    } else if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      if (state.run.status === "running") stopRun("停止");
      else startRun(COMMANDS.find((c) => c.name === state.selected));
    } else if ((e.ctrlKey || e.metaKey) && e.code === "Space") {
      e.preventDefault();
      pauseRun();
    } else if (e.key === "Escape" && state.run.status === "running" && !document.querySelector("dialog[open]") && !/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || "")) {
      stopRun("停止");
    }
  });

  // dock resize
  const grip = $("#dockGrip");
  let dragY = null;
  let dragH = 0;
  grip.addEventListener("pointerdown", (e) => {
    dragY = e.clientY;
    dragH = $("#dock").getBoundingClientRect().height;
    grip.setPointerCapture(e.pointerId);
    document.body.style.cursor = "row-resize";
  });
  grip.addEventListener("pointermove", (e) => {
    if (dragY === null) return;
    const next = Math.min(window.innerHeight * 0.6, Math.max(96, dragH - (e.clientY - dragY)));
    document.documentElement.style.setProperty("--dock-h", `${next}px`);
  });
  grip.addEventListener("pointerup", () => {
    dragY = null;
    document.body.style.cursor = "";
  });

  // pad drag
  const head = $("#padHead");
  let off = null;
  head.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button")) return;
    const r = pad.getBoundingClientRect();
    off = { x: e.clientX - r.left, y: e.clientY - r.top };
    pad.style.left = `${r.left}px`;
    pad.style.top = `${r.top}px`;
    pad.style.right = "auto";
    pad.style.bottom = "auto";
    head.setPointerCapture(e.pointerId);
  });
  head.addEventListener("pointermove", (e) => {
    if (!off) return;
    pad.style.left = `${Math.max(4, e.clientX - off.x)}px`;
    pad.style.top = `${Math.max(48, e.clientY - off.y)}px`;
  });
  head.addEventListener("pointerup", () => {
    off = null;
  });

  // remote pad events (pop-out window and other tabs)
  const onRemote = (m) => {
    if (m?.type !== "btn") return;
    if (m.down) press(m.name, m.source, true);
    else release(m.name, m.source, true);
  };
  if (bc) bc.onmessage = (ev) => onRemote(ev.data);
  window.addEventListener("message", (e) => {
    if (e.origin === location.origin) onRemote(e.data);
  });

  // audio meter animation
  setInterval(() => {
    state.audio = Math.max(0.06, Math.min(0.95, state.audio + (Math.random() - 0.5) * 0.16));
    const pct = Math.round(state.audio * 100);
    $("#audioBar").style.width = `${pct}%`;
    $("#audioBar2").style.width = `${pct}%`;
    const db = Math.round(20 * Math.log10(state.audio * (state.volume / 100)));
    const label = `${db} dB`;
    $("#audioDb").textContent = label;
    $("#audioDb2").textContent = label;
  }, 420);

  const initial = location.hash.slice(1);
  if (["run", "history", "devices", "diag"].includes(initial)) setView(initial, false);
  window.addEventListener("hashchange", () => {
    const v = location.hash.slice(1);
    if (["run", "history", "devices", "diag"].includes(v) && v !== state.view) setView(v, false);
  });

  // initial status
  $("#selTransport").value = state.transport;
  $("#selBaud").value = String(state.baud);
  applyTransport();
  $("#selArb").value = "human";
  $("#btnConnect").textContent = "切断";
  $("#btnConnect").classList.remove("primary");
}

init();
