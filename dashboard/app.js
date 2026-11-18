"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  data: null,
  period: "today",
  segment: "All",
  page: "overview",
  query: "",
  expanded: false,
};
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const rupee = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});
const quantity = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
const escapeHtml = (text) =>
  String(text ?? "—").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const money = (value) => (value == null ? "—" : rupee.format(value));
const signed = (value) => (value > 0 ? "+" : "") + money(value);
const tone = (value) => (value < 0 ? "negative" : value > 0 ? "positive" : "");
const dateKey = (date) =>
  new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
const timeLabel = (stamp) => (stamp ? stamp.slice(11, 16) : "—");
const readable = (text) =>
  String(text ?? "")
    .replaceAll("_", " ")
    .toLowerCase();
const animations = new WeakMap();
let chartState = { points: [], progress: 1, hover: -1, frame: null };
const performanceNavigation = createChartNavigation($("chart"), {
  length: () => chartState.points.length,
  plot: (width) => ({ left: 57, right: width - 13 }),
  controls: "performance-navigation",
  unit: "closes",
  minCount: 2,
  onChange: () => {
    chartState.hover = -1;
    chartState.progress = 1;
    $("chart-tooltip").hidden = true;
    drawChart();
  },
});
function visiblePerformancePoints() {
  const range = performanceNavigation.range();
  return chartState.points.slice(range.start, range.end);
}

function html(id, value) {
  if ($(id).innerHTML !== value) $(id).innerHTML = value;
}

function animateNumber(id, value, formatter = money) {
  const node = $(id);
  if (value == null) {
    node.textContent = "—";
    return;
  }
  if (node.dataset.value === String(value)) return;
  const start = Number(node.dataset.value || 0);
  node.dataset.value = value;
  cancelAnimationFrame(animations.get(node));
  if (reducedMotion.matches) {
    node.textContent = formatter(value);
    return;
  }
  const started = performance.now();
  function frame(now) {
    const progress = Math.min(1, (now - started) / 650);
    node.textContent = formatter(
      start + (value - start) * (1 - (1 - progress) ** 4),
    );
    if (progress < 1) animations.set(node, requestAnimationFrame(frame));
  }
  animations.set(node, requestAnimationFrame(frame));
}

function periodTrades() {
  if (!state.data) return [];
  const today =
    state.data.trades
      .map((t) => t.closed.slice(0, 10))
      .filter(Boolean)
      .sort()
      .at(-1) || dateKey(new Date(state.data.as_of));
  const first = dateKey(
    new Date(new Date(state.data.as_of).getTime() - 6 * 86400000),
  );
  return state.data.trades.filter(
    (t) =>
      state.period === "all" ||
      (state.period === "today"
        ? t.closed.slice(0, 10) === today
        : t.closed.slice(0, 10) >= first && t.closed.slice(0, 10) <= today),
  );
}

function render() {
  const { data } = state;
  if (!data) return;
  const trades = periodTrades();
  const latest = data.trades
    .map((t) => t.closed.slice(0, 10))
    .filter(Boolean)
    .sort()
    .at(-1);
  $("subtitle").textContent =
    state.period === "today" && latest
      ? new Date(latest + "T12:00:00+05:30").toLocaleDateString("en-IN", {
          timeZone: "Asia/Kolkata",
          day: "numeric",
          month: "long",
          year: "numeric",
        }) + " · Latest PAPER session"
      : state.period === "week"
        ? "Last seven calendar days · PAPER results"
        : "Recorded PAPER trading history";
  const net = trades.reduce((sum, t) => sum + t.net, 0);
  const gross = trades.reduce((sum, t) => sum + t.gross, 0);
  const wins = trades.filter((t) => t.net > 0).length;
  const losses = trades.filter((t) => t.net < 0).length;
  const wallets = data.accounts.filter((a) => a.balance != null);
  const balance = wallets.reduce((sum, a) => sum + a.balance, 0);
  animateNumber("balance", wallets.length ? balance : null);
  animateNumber("pnl", net, signed);
  $("pnl").className = "stat-value " + tone(net);
  $("account-count").textContent = wallets.length;
  $("pnl-foot").textContent = money(gross - net) + " estimated charges";
  animateNumber("trade-count", trades.length, (v) =>
    Math.round(v).toString().padStart(2, "0"),
  );
  $("trade-foot").textContent = `${wins} profitable · ${losses} losing`;
  animateNumber(
    "win-rate",
    trades.length ? (wins / trades.length) * 100 : null,
    (v) => v.toFixed(1) + "%",
  );
  $("win-foot").textContent = trades.length
    ? `${wins} of ${trades.length} completed trades`
    : "No completed trades this period";
  animateNumber("chart-total", net, signed);
  $("chart-total").className = tone(net);
  html(
    "wallets",
    data.accounts
      .map(
        (a) =>
          `<div class="wallet"><div class="wallet-top"><span class="wallet-name"><i></i>${escapeHtml(a.name)}</span><span class="wallet-value">${money(a.balance)}</span></div><div class="wallet-track"><div style="width:${balance && a.balance != null ? Math.max(0, (a.balance / balance) * 100) : 0}%"></div></div></div>`,
      )
      .join(""),
  );
  renderTrades();
  renderEngines();
  $("positions-section").hidden = !data.positions.length;
  html(
    "positions",
    data.positions
      .map(
        (p) =>
          `<div class="position-row"><strong>${escapeHtml(p.symbol)} ${escapeHtml(p.contract)}</strong><span>Entry ${money(p.entry)}</span><span>Qty ${escapeHtml(p.qty)}</span><span>SL ${money(p.stop)}</span></div>`,
      )
      .join(""),
  );
  $("errors").hidden = !data.errors.length;
  $("errors").textContent = data.errors.join(" · ");
  $("updated").textContent =
    "Synced " +
    new Date(data.as_of).toLocaleTimeString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }) +
    " IST";
  updateChart(trades);
}

function renderTrades() {
  let trades = periodTrades().filter(
    (t) =>
      (state.segment === "All" || t.segment === state.segment) &&
      `${t.symbol} ${t.contract} ${t.source}`
        .toLowerCase()
        .includes(state.query),
  );
  $("table-count").textContent = trades.length;
  const total = trades.length;
  if (state.page === "overview" && !state.expanded) trades = trades.slice(0, 6);
  html(
    "trades",
    trades
      .map(
        (t, i) =>
          `<tr style="animation-delay:${Math.min(i, 10) * 22}ms"><td><span class="instrument-icon">${escapeHtml(t.symbol.slice(0, 2))}</span><div class="instrument-copy"><strong>${escapeHtml(t.symbol)}</strong><small>${escapeHtml(t.contract)} · ${escapeHtml(t.segment)}</small></div></td><td><span class="source-label">${escapeHtml(t.source)}</span></td><td class="mono">${money(t.entry)}</td><td class="mono">${money(t.exit)}</td><td class="mono">${escapeHtml(t.qty)}<small style="color:#748279;margin-top:4px;font-size:8px">${escapeHtml(t.lots)} lot${t.lots === 1 ? "" : "s"}</small></td><td class="mono ${tone(t.net)}">${signed(t.net)}</td><td title="${escapeHtml(t.closed)}">${state.period === "today" ? "" : escapeHtml(t.closed.slice(5, 10)) + " · "}${escapeHtml(timeLabel(t.closed))}</td></tr>`,
      )
      .join(""),
  );
  $("empty-trades").hidden = trades.length > 0;
  $("table-note").textContent =
    `Showing ${trades.length} of ${total} completed trades`;
  $("view-all").hidden =
    state.page === "trades" || state.expanded || total <= 6;
}

function renderEngines() {
  const items = state.data.markets;
  html(
    "engines",
    items.length
      ? items
          .map(
            (m) =>
              `<article class="engine"><div class="engine-top"><strong>${escapeHtml(m.symbol)}</strong><span class="engine-status">${m.age == null ? "UNAVAILABLE" : m.age <= 90 ? "RECENT SNAPSHOT" : "LAST RECORDED"}</span></div><div class="engine-price">${m.metric ? escapeHtml(m.metric) : m.spot == null ? "—" : quantity.format(m.spot)}</div><div class="engine-reason">${escapeHtml(m.reason)}</div><div class="engine-bottom"><span>${escapeHtml(readable(m.action))}</span>${m.regime ? `<span>${escapeHtml(readable(m.regime))}</span>` : ""}<span>${m.age == null ? "No timestamp" : m.age < 60 ? m.age + "s ago" : Math.floor(m.age / 60) + "m ago"}</span></div></article>`,
          )
          .join("")
      : '<article class="engine"><h2>Awaiting telemetry</h2><p class="engine-reason">Engine decisions appear when telemetry is available.</p></article>',
  );
}

function updateChart(trades) {
  let cumulative = 0;
  const points = [
    { value: 0, label: "Period start" },
    ...trades
      .slice()
      .reverse()
      .map((t) => ({ value: (cumulative += t.net), label: t.closed })),
  ];
  const signature = JSON.stringify(points);
  $("chart-empty").hidden = trades.length > 0;
  if (signature === chartState.signature) return;
  cancelAnimationFrame(chartState.frame);
  chartState = {
    points,
    signature,
    progress: reducedMotion.matches ? 1 : 0,
    hover: -1,
    frame: null,
  };
  $("chart-start").textContent = trades.length
    ? state.period === "today"
      ? "Session start"
      : trades.at(-1).closed.slice(0, 10)
    : "Session start";
  $("chart-end").textContent = trades.length
    ? timeLabel(trades[0].closed) + " IST · Last close"
    : "Awaiting a close";
  const start = performance.now();
  function frame(now) {
    chartState.progress = reducedMotion.matches
      ? 1
      : Math.min(1, (now - start) / 900);
    drawChart();
    if (chartState.progress < 1)
      chartState.frame = requestAnimationFrame(frame);
  }
  chartState.frame = requestAnimationFrame(frame);
}

function drawChart() {
  const canvas = $("chart");
  const { width, height } = canvas.getBoundingClientRect();
  if (!width || !height) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
  if (
    canvas.width !== Math.round(width * dpr) ||
    canvas.height !== Math.round(height * dpr)
  ) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const points = visiblePerformancePoints();
  performanceNavigation.sync();
  const axisLabel = (point) =>
    point?.label === "Period start"
      ? "Period start"
      : point
        ? point.label.slice(5, 10) + " · " + timeLabel(point.label) + " IST"
        : "—";
  if ($("chart-start").textContent !== axisLabel(points[0]))
    $("chart-start").textContent = axisLabel(points[0]);
  if ($("chart-end").textContent !== axisLabel(points.at(-1)))
    $("chart-end").textContent = axisLabel(points.at(-1));
  if (!points.length) return;
  const values = points.map((p) => p.value);
  let min = Math.min(...values),
    max = Math.max(...values);
  const padding = Math.max(100, (max - min) * 0.2);
  min -= padding;
  max += padding;
  const left = 57,
    right = width - 13,
    top = 12,
    bottom = height - 13;
  const x = (i) => left + (i / Math.max(1, points.length - 1)) * (right - left);
  const y = (value) => bottom - ((value - min) / (max - min)) * (bottom - top);
  ctx.font = '9px "IBM Plex Mono", monospace';
  ctx.textBaseline = "middle";
  for (let i = 0; i < 4; i++) {
    const value = max - ((max - min) * i) / 3,
      yy = y(value);
    ctx.strokeStyle = "#1a251d";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 5]);
    ctx.beginPath();
    ctx.moveTo(left, yy);
    ctx.lineTo(right, yy);
    ctx.stroke();
    ctx.fillStyle = "#617569";
    ctx.fillText(
      (value < 0 ? "−" : "") +
        (Math.abs(value) >= 1000
          ? (Math.abs(value) / 1000).toFixed(1) + "k"
          : Math.abs(value).toFixed(0)),
      4,
      yy,
    );
  }
  ctx.setLineDash([]);
  if (points.length < 2) return;
  const color = points.at(-1).value < 0 ? "#e4a3a7" : "#b8f5cc";
  const progress = 1 - (1 - chartState.progress) ** 3;
  ctx.save();
  ctx.beginPath();
  ctx.rect(left - 3, 0, (right - left + 6) * progress, height);
  ctx.clip();
  const gradient = ctx.createLinearGradient(0, top, 0, bottom);
  gradient.addColorStop(0, points.at(-1).value < 0 ? "#c16c6f25" : "#8bd5a225");
  gradient.addColorStop(1, "#00000000");
  ctx.beginPath();
  points.forEach((p, i) =>
    i ? ctx.lineTo(x(i), y(p.value)) : ctx.moveTo(x(i), y(p.value)),
  );
  ctx.lineTo(x(points.length - 1), bottom);
  ctx.lineTo(left, bottom);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();
  ctx.beginPath();
  points.forEach((p, i) =>
    i ? ctx.lineTo(x(i), y(p.value)) : ctx.moveTo(x(i), y(p.value)),
  );
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.8;
  ctx.lineJoin = "round";
  ctx.stroke();
  const last = points.at(-1);
  ctx.beginPath();
  ctx.arc(right, y(last.value), 3, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.restore();
  if (chartState.hover >= 0 && points[chartState.hover]) {
    const i = chartState.hover;
    ctx.beginPath();
    ctx.moveTo(x(i), top);
    ctx.lineTo(x(i), bottom);
    ctx.strokeStyle = "#4b6152";
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(x(i), y(points[i].value), 4, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
  }
}

$("chart").addEventListener("pointermove", (event) => {
  const points = visiblePerformancePoints();
  if (points.length < 2 || performanceNavigation.busy()) return;
  const bounds = event.currentTarget.getBoundingClientRect();
  const index = Math.max(
    0,
    Math.min(
      points.length - 1,
      Math.round(
        ((event.clientX - bounds.left - 57) / (bounds.width - 70)) *
          (points.length - 1),
      ),
    ),
  );
  chartState.hover = index;
  const point = points[index];
  $("chart-tooltip").textContent = `${point.label} · ${signed(point.value)}`;
  $("chart-tooltip").hidden = false;
  $("chart-tooltip").style.left =
    Math.max(
      0,
      Math.min(event.clientX - bounds.left + 10, bounds.width - 240),
    ) + "px";
  $("chart-tooltip").style.top = "5px";
  if (!chartState.pointerFrame)
    chartState.pointerFrame = requestAnimationFrame(() => {
      chartState.pointerFrame = null;
      drawChart();
    });
});
$("chart").addEventListener("pointerleave", () => {
  chartState.hover = -1;
  $("chart-tooltip").hidden = true;
  drawChart();
});
new ResizeObserver(() => drawChart()).observe($("chart"));

document.querySelectorAll("[data-period]").forEach((button) =>
  button.addEventListener("click", () => {
    state.period = button.dataset.period;
    performanceNavigation.reset();
    document
      .querySelectorAll("[data-period]")
      .forEach((b) => b.classList.toggle("selected", b === button));
    render();
  }),
);
document.querySelectorAll("[data-segment]").forEach((button) =>
  button.addEventListener("click", () => {
    state.segment = button.dataset.segment;
    document
      .querySelectorAll("[data-segment]")
      .forEach((b) => b.classList.toggle("selected", b === button));
    renderTrades();
  }),
);
$("search").addEventListener("input", (event) => {
  state.query = event.target.value.toLowerCase();
  renderTrades();
});
document.querySelectorAll("[data-page]").forEach((button) =>
  button.addEventListener("click", () => {
    state.page = button.dataset.page;
    document
      .querySelectorAll("[data-page]")
      .forEach((b) => b.classList.toggle("active", b === button));
    const names = {
      overview: ["Overview", "Trading workspace"],
      trades: ["Trade history", "Every trade, accounted for"],
      intelligence: ["Intelligence", "Inside the engines"],
    };
    $("page-name").textContent = names[state.page][0];
    $("heading").innerHTML = names[state.page][1] + "<span>.</span>";
    $("overview-content").hidden = state.page !== "overview";
    $("trade-section").hidden = state.page === "intelligence";
    $("intelligence-section").hidden = state.page === "trades";
    renderTrades();
    requestAnimationFrame(drawChart);
  }),
);
$("view-all").addEventListener("click", () => {
  state.expanded = true;
  renderTrades();
});
let loading = false;
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    if (!response.ok) throw new Error("Snapshot unavailable");
    state.data = await response.json();
    $("connection").className = "connection";
    $("connection").innerHTML = "<i></i> Data connected";
    render();
  } catch (error) {
    $("connection").className = "connection offline";
    $("connection").innerHTML = "<i></i> Connection lost";
    $("errors").hidden = false;
    $("errors").textContent =
      "Unable to refresh. Showing the last available snapshot.";
  } finally {
    loading = false;
  }
}
$("refresh").addEventListener("click", refresh);
function clock() {
  $("clock").textContent =
    new Date().toLocaleTimeString("en-GB", {
      timeZone: "Asia/Kolkata",
      hour12: false,
    }) + " IST";
}
clock();
setInterval(clock, 1000);
setInterval(() => {
  if (!document.hidden) refresh();
}, 5000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
refresh();

// Move a small composited reflection, leaving the frosted surface itself static.
let reflectedSurface = null;
let reflectionFrame = null;
let reflectionPoint = null;
function clearReflection() {
  reflectedSurface?.classList.remove("glass-active");
  reflectedSurface = null;
}
document.addEventListener(
  "pointermove",
  (event) => {
    if (reducedMotion.matches || event.pointerType === "touch") return;
    const surface = event.target.closest(".stat, .panel, .engine");
    if (surface !== reflectedSurface) {
      clearReflection();
      reflectedSurface = surface;
      if (surface) {
        if (!surface.querySelector(":scope > .glass-reflection")) {
          const reflection = document.createElement("span");
          reflection.className = "glass-reflection";
          reflection.setAttribute("aria-hidden", "true");
          surface.append(reflection);
        }
        surface.classList.add("glass-active");
      }
    }
    if (!surface) return;
    reflectionPoint = { x: event.clientX, y: event.clientY };
    if (reflectionFrame) return;
    reflectionFrame = requestAnimationFrame(() => {
      reflectionFrame = null;
      if (!reflectedSurface || !reflectionPoint) return;
      const bounds = reflectedSurface.getBoundingClientRect();
      const reflection = reflectedSurface.querySelector(
        ":scope > .glass-reflection",
      );
      reflection.style.transform = `translate3d(${reflectionPoint.x - bounds.left - 110}px, ${reflectionPoint.y - bounds.top - 110}px, 0)`;
    });
  },
  { passive: true },
);
document.addEventListener("pointerleave", clearReflection);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearReflection();
});

// A short, composited pulse gives every control the same tactile feedback.
document.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button || button.disabled || reducedMotion.matches) return;
  button.querySelector(":scope > .control-pulse")?.remove();
  const bounds = button.getBoundingClientRect();
  const pulse = document.createElement("span");
  const size = Math.max(bounds.width, bounds.height) * 1.8;
  const x = event.detail ? event.clientX - bounds.left : bounds.width / 2;
  const y = event.detail ? event.clientY - bounds.top : bounds.height / 2;
  pulse.className = "control-pulse";
  pulse.setAttribute("aria-hidden", "true");
  pulse.style.width = pulse.style.height = size + "px";
  pulse.style.left = x - size / 2 + "px";
  pulse.style.top = y - size / 2 + "px";
  button.append(pulse);
  pulse.addEventListener("animationend", () => pulse.remove(), { once: true });
});
