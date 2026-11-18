/* The workspace consumes recorded evidence; it never submits a trading action. */

(() => {
  const desk = {
    data: null,

    bot: "Indices",

    symbol: "NIFTY",

    overlays: { levels: true, zones: true, events: true },

    bar: -1,

    focusedLevel: null,

    frame: null,

    signature: "",

    loading: false,
  };

  const el = (id) => document.getElementById(id);

  const safe = (value) => escapeHtml(value);

  const timestamp = (epoch) =>
    epoch
      ? new Date(epoch * 1000).toLocaleString("en-IN", {
          timeZone: "Asia/Kolkata",

          day: "2-digit",

          month: "short",

          hour: "2-digit",

          minute: "2-digit",

          hour12: false,
        }) + " IST"
      : "Not recorded";

  const candleClock = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",

    hour: "2-digit",

    minute: "2-digit",

    hour12: false,
  });

  const candleTime = (epoch) => candleClock.format(new Date(epoch * 1000));

  const pretty = (text) =>
    String(text || "Not recorded")
      .replaceAll("_", " ")

      .toLowerCase();

  const record = () => desk.data?.engines[desk.bot]?.[desk.symbol];

  let cachedBarsSource = null;

  let cachedBars = [];

  const allBars = () => {
    const bars = record()?.candles || [];

    if (bars === cachedBarsSource) return cachedBars;

    cachedBarsSource = bars;

    if (!bars.length) {
      cachedBars = [];

      return cachedBars;
    }

    cachedBars = bars;

    return cachedBars;
  };

  const candleNavigation = createChartNavigation(el("structure-chart"), {
    length: () => allBars().length,

    plot: (width) => ({ left: 12, right: width - 66 }),

    controls: "structure-navigation",

    unit: "candles",

    minCount: 8,

    initialCount: 75,

    onChange: () => {
      desk.bar = -1;

      renderDecision(record());

      el("candle-readout").textContent =
        "Scroll to zoom · drag to pan · double-click to reset";

      drawCandles();
    },
  });

  const selectedBars = () => {
    const range = candleNavigation.range();

    return allBars().slice(range.start, range.end);
  };

  function put(id, content) {
    if (el(id).innerHTML !== content) el(id).innerHTML = content;
  }

  function selectBot(bot, symbol) {
    desk.bot = bot;

    const records = desk.data?.engines[bot] || {};

    desk.symbol =
      symbol || Object.keys(records)[0] || (bot === "Indices" ? "NIFTY" : "");

    desk.bar = -1;

    desk.focusedLevel = null;

    candleNavigation.reset();

    el("bot-workspace").dataset.activeBot = bot;

    document.querySelectorAll("[data-bot]").forEach((button) => {
      const active = button.dataset.bot === bot;

      button.classList.toggle("active", active);

      button.setAttribute("aria-selected", String(active));
    });

    document.querySelector(`[data-segment="${bot}"]`)?.click();

    renderWorkspace();

    if (!reducedMotion.matches) {
      document

        .querySelectorAll(".desk-grid, .levels-strip, .execution-trail")

        .forEach((surface, i) => {
          surface.getAnimations().forEach((animation) => animation.cancel());

          surface.animate(
            [
              { opacity: 0.35, transform: "translateY(9px)" },

              { opacity: 1, transform: "translateY(0)" },
            ],

            {
              duration: 380,

              delay: i * 28,

              easing: "cubic-bezier(.16,1,.3,1)",
            },
          );
        });
    }
  }

  function renderBotCards() {
    const trades = state.data ? periodTrades() : [];

    for (const bot of ["Indices", "Stocks", "Commodities"]) {
      const closed = trades.filter((trade) => trade.segment === bot);

      const net = closed.reduce((sum, trade) => sum + trade.net, 0);

      const pnl = document.querySelector(`[data-bot-pnl="${bot}"]`);

      pnl.textContent = signed(net);

      pnl.className = tone(net);

      document.querySelector(`[data-bot-count="${bot}"]`).textContent =
        closed.length;

      const observations = Object.values(desk.data?.engines[bot] || {});

      const latest = Math.max(
        0,

        ...observations.map((item) => item.observed_epoch || 0),
      );

      document.querySelector(
        `[data-bot="${bot}"] .bot-card-status`,
      ).textContent = !latest
        ? "Awaiting observations"
        : Date.now() / 1000 - latest <= 90
          ? "Recent observations"
          : "Saved observations · " + timestamp(latest).split(" IST")[0];
    }
  }

  function step(label, status, detail, mode = "") {
    return `<div class="decision-step ${mode}"><strong>${safe(label)}</strong><em>${safe(status)}</em><p>${safe(detail)}</p></div>`;
  }

  function renderDecision(item) {
    const a = item?.analysis || {};

    const structure = a.market_structure || {};

    const membership = a.scanner_membership;

    const confirmation = a.breakout_confirmation;

    const liquidity = a.mtf_liquidity;

    const quality = structure.trade_quality || a.trade_quality || a.bsm_quality;

    const decision = a.entry_decision || {
      action: a.recommended_dir || "WAIT",

      reason: a.reason || "No decision recorded for this observation",
    };

    el("decision-action").textContent = pretty(decision.action).toUpperCase();

    el("decision-context").textContent =
      "Evidence at " + timestamp(item?.observed_epoch);

    let steps = "";

    if (desk.bot === "Stocks") {
      steps += step(
        "Scanner membership",

        membership ? (membership.allowed ? "MATCHED" : "OUTSIDE") : "UNKNOWN",

        membership?.reason || "Scanner evidence unavailable",

        membership?.allowed ? "pass" : "",
      );
    } else {
      steps += step(
        "Market context",

        a.regime_info || a.regime ? "RECORDED" : "UNKNOWN",

        pretty(a.regime_info?.regime || a.regime),

        a.regime_info || a.regime ? "pass" : "",
      );
    }

    const confirmed =
      confirmation?.closed_candle ||
      structure.allowed ||
      (a.setup && a.setup !== "NONE" && a.recommended_dir !== "HOLD");

    steps += step(
      "Price structure",

      confirmed ? "CONFIRMED" : "WAIT",

      confirmation?.entry_path
        ? pretty(confirmation.entry_path)
        : structure.reason || a.reason || "No confirmed price event recorded",

      confirmed ? "pass" : "hold",
    );

    if (desk.bot === "Stocks") {
      steps += step(
        "Liquidity & reversal zones",

        !liquidity
          ? "UNKNOWN"
          : liquidity.liquidity_clear
            ? "CLEAR"
            : "BLOCKED",

        liquidity?.liquidity_sweep_bounce
          ? "High-volume sweep and reclaim recorded"
          : liquidity?.reasons?.join(" · ") ||
              (liquidity
                ? "No mapped opposing zone within the required room"
                : "Zone map was not saved for this observation"),

        liquidity?.liquidity_clear ? "pass" : liquidity ? "hold" : "",
      );
    } else {
      const checks = structure.direction_checks || {};

      const info = Object.entries(checks)

        .map(
          ([direction, check]) =>
            `${pretty(direction)}: ${check.reason || "no result"}`,
        )

        .join(" · ");

      steps += step(
        "Directional confirmation",

        checks.BUY_CE?.allowed || checks.BUY_PE?.allowed ? "SUPPORTED" : "WAIT",

        info ||
          a.trade_quality?.reason ||
          "Same-strike OI or directional evidence is not saved here",

        checks.BUY_CE?.allowed || checks.BUY_PE?.allowed ? "pass" : "",
      );
    }

    const evaluated = quality && quality.reason !== "NO_ENTRY_CANDIDATE";

    const passed = quality?.allowed ?? quality?.greek_supported;

    steps += step(
      "Executable option quality",

      !evaluated ? "NOT RECORDED" : passed ? "PASSED" : "HOLD",

      evaluated
        ? (quality.failure_factors || quality.reasons || []).join(" · ") ||
            quality.reason ||
            `Quality score ${quality.score ?? "not recorded"}`
        : "Greek, spread and depth results appear when the engine evaluates a contract",

      evaluated ? (passed ? "pass" : "hold") : "",
    );

    steps += step(
      "Signal / entry decision",

      String(decision.action || "WAIT").toUpperCase(),

      decision.reason || "No execution decision recorded",

      /^BUY_|CANDIDATE/.test(decision.action) ? "pass" : "hold",
    );

    put("decision-trail", steps);

    el("decision-verdict").textContent =
      decision.reason || "No decision evidence available.";
  }

  function renderLevels(item) {
    const spot =
      item?.analysis?.spot ||
      item?.analysis?.spot_ltp ||
      selectedBars().at(-1)?.close ||
      0;

    const levels = [...(item?.levels || [])]

      .sort((a, b) => Math.abs(a.price - spot) - Math.abs(b.price - spot))

      .slice(0, 16);

    put(
      "structure-levels",

      levels.length
        ? levels

            .map(
              (level, i) =>
                `<button class="level-chip ${level.role === "SUPPLY" ? "resistance" : ""}" data-timeframe="${safe(level.timeframe)}" data-level="${i}">${quantity.format(level.price)}<small>${safe(level.timeframe)} · ${safe(level.label)}</small></button>`,
            )

            .join("")
        : '<span class="level-empty">No support, resistance or liquidity map was recorded for this observation.</span>',
    );

    el("structure-levels")
      .querySelectorAll("[data-level]")

      .forEach((button) =>
        button.addEventListener("click", () => {
          desk.focusedLevel = levels[Number(button.dataset.level)].price;

          el("structure-levels")
            .querySelectorAll("button")

            .forEach((b) => b.classList.toggle("selected", b === button));

          drawCandles();
        }),
      );
  }

  function renderExecutions() {
    const trades = state.data
      ? periodTrades()
          .filter(
            (t) =>
              t.segment === desk.bot &&
              (!desk.symbol || t.symbol === desk.symbol),
          )

          .slice(0, 6)
      : [];

    el("execution-caption").textContent =
      desk.symbol + " · Completed PAPER trades in selected period";

    const open = (state.data?.positions || []).filter(
      (p) => p.segment === desk.bot && p.symbol === desk.symbol,
    );

    const openCards = open

      .map(
        (p) =>
          `<article class="execution-card"><div class="execution-card-top"><strong>${safe(p.contract)}</strong><span class="positive">SAVED OPEN POSITION</span></div><p>Entry ${money(p.entry)} · Qty ${safe(p.qty)}</p><div class="execution-prices"><span>Current SL ${money(p.stop)}</span><span>${safe(timeLabel(p.opened))} IST</span></div></article>`,
      )

      .join("");

    put(
      "execution-trail",

      openCards +
        (trades.length
          ? trades

              .map(
                (trade) =>
                  `<article class="execution-card"><div class="execution-card-top"><strong>${safe(trade.contract)}</strong><span class="${tone(trade.net)} mono">${signed(trade.net)}</span></div><p>${safe(trade.source)} · ${safe(timeLabel(trade.opened))} → ${safe(timeLabel(trade.closed))} IST</p><div class="execution-prices"><span>Entry ${money(trade.entry)}</span><span>Exit ${money(trade.exit)}</span></div><p>Initial SL ${money(trade.initial_stop)} · Target ${money(trade.target)}</p><div class="execution-reason">${safe(pretty(trade.reason))}</div><p>${safe(trade.lots)} lots · ${safe(trade.qty)} quantity · ${money(trade.charges)} estimated charges</p></article>`,
              )

              .join("")
          : open.length
            ? ""
            : '<div class="execution-empty">No completed trades for this instrument in the selected reporting period.</div>'),
    );
  }

  function renderScanners() {
    const live = desk.data?.live_scanner || {};
    const records = Object.values(live.symbols || {}).filter(
      (r) => r.analysis?.scanner_membership?.allowed,
    );
    const matches = ["LIVE", "LAST_RECORDED"].includes(live.status) ? records : [];
    el("scanner-observed").textContent =
      `${live.status || "CONNECTING"} · ${live.observed_epoch ? timestamp(live.observed_epoch) : live.message || "Waiting for live quotes"} · ${live.status === "LAST_RECORDED" ? "Saved after-hours snapshot" : "3s refresh"}`;
    el("scanner-coverage").textContent =
      `${live.message || ""} · ${live.fresh_quote_count ?? 0}/${live.universe_count ?? 0} fresh quotes · ${live.pending_references ?? 0} references loading${live.complete ? " · Complete scan" : " · Scan incomplete / market closed"}. ${live.status === "LAST_RECORDED" ? "Last saved results; prices are not live." : "Live-session results only."}`;

    for (const [side, id, count] of [
      ["CE", "bullish-scanner", "bull-count"],
      ["PE", "bearish-scanner", "bear-count"],
    ]) {
      const filtered = matches
        .filter((r) => r.analysis.scanner_membership.opt_type === side)
        .sort((a, b) =>
          side === "CE"
            ? b.analysis.scanner_membership.day_change_pct -
              a.analysis.scanner_membership.day_change_pct
            : a.analysis.scanner_membership.day_change_pct -
              b.analysis.scanner_membership.day_change_pct,
        );
      el(count).textContent = live.complete ? filtered.length : (filtered.length ? filtered.length + "+" : "—");
      const numeric = (v) =>
        Number.isFinite(Number(v)) && v != null
          ? quantity.format(Number(v))
          : "—";
      const cells = filtered
        .map((r) => {
          const m = r.analysis.scanner_membership;
          const breakout = (r.events || [])
            .filter((e) => e.type === "BREAKOUT")
            .at(-1);
          const change =
            m.price && m.previous_close ? m.price - m.previous_close : null;
          return `<tr class="${desk.symbol === r.symbol && desk.bot === "Stocks" ? "selected-stock" : ""}"><td><button class="scanner-stock-link" data-stock="${safe(r.symbol)}">${safe(r.symbol)} <small>NSE</small></button></td><td>${numeric(m.price)}</td><td class="${tone(change)}">${numeric(change)}</td><td class="${tone(m.day_change_pct)}">${numeric(m.day_change_pct)}%</td><td>${numeric(m.session_volume)}</td><td>${numeric(m.previous_session_volume)}</td><td>${numeric(m.previous_ema_15m)}</td><td>${breakout ? safe(timestamp(breakout.timestamp)) : "Not recorded"}</td><td>${safe(timestamp(r.observed_epoch))}</td></tr>`;
        })
        .join("");
      put(
        id,
        `<div class="scanner-filters"><span>NSE</span><span>F&amp;O Stocks</span><span>Volume &gt; Previous Volume</span><span>Price %Change ${side === "CE" ? "≥ 2.00" : "≤ −2.00"}</span><span>Price ${side === "CE" ? "&gt;" : "&lt;"} Previous EMA (10, 15 Min)</span></div><table class="scanner-table"><thead><tr><th>Name · open chart</th><th>Price</th><th>Day Price Change</th><th>Change %</th><th>Volume</th><th>Previous Volume</th><th>Previous EMA (10, 15 Min)</th><th>Breakout bar · IST</th><th>Observed · IST</th></tr></thead><tbody>${cells || (live.complete ? '<tr><td colspan="9">No stocks matched this side in the completed scan.</td></tr>' : '<tr><td colspan="9">Scanner data unavailable or still loading. This does not mean zero stocks qualified.</td></tr>')}</tbody></table>`,
      );
    }

    document

      .querySelectorAll("[data-stock]")

      .forEach((button) =>
        button.addEventListener("click", () =>
          (() => {
            selectBot("Stocks", button.dataset.stock);

            const event = (record()?.events || [])
              .filter((e) => e.type === "BREAKOUT")
              .at(-1);

            if (event) {
              candleNavigation.reveal(
                allBars().findIndex((b) => b.timestamp === event.timestamp),
              );

              requestAnimationFrame(() =>
                inspect(
                  selectedBars().findIndex(
                    (b) => b.timestamp === event.timestamp,
                  ),
                ),
              );
            }

            el("structure-chart").scrollIntoView({
              behavior: reducedMotion.matches ? "instant" : "smooth",
              block: "center",
            });
          })(),
        ),
      );
  }

  function renderWorkspace() {
    if (!desk.data) return;

    renderBotCards();

    const records = desk.data.engines[desk.bot] || {};

    if (!records[desk.symbol]) desk.symbol = Object.keys(records)[0] || "";

    put(
      "desk-symbol",

      Object.keys(records).length
        ? (desk.bot === "Stocks" ? [desk.symbol] : Object.keys(records))

            .map(
              (name) => `<option value="${safe(name)}">${safe(name)}</option>`,
            )

            .join("")
        : '<option value="">Awaiting instrument data</option>',
    );

    el("desk-symbol").value = desk.symbol;
    el("desk-symbol").hidden = desk.bot === "Stocks";
    let selectedName = el("selected-stock-name");
    if (!selectedName) {
      selectedName = document.createElement("strong");
      selectedName.id = "selected-stock-name";
      el("desk-symbol").after(selectedName);
    }
    selectedName.hidden = desk.bot !== "Stocks";
    selectedName.textContent = desk.symbol + " · NSE";
    let liveLink = el("live-stock-chart");
    if (!liveLink) {
      liveLink = document.createElement("a");
      liveLink.id = "live-stock-chart";
      liveLink.target = "_blank";
      liveLink.rel = "noopener noreferrer";
      el("structure-empty").append(liveLink);
    }
    liveLink.hidden = desk.bot !== "Stocks";
    liveLink.textContent = "Open " + desk.symbol + " live chart ↗";
    liveLink.href =
      "https://www.tradingview.com/chart/?symbol=" +
      encodeURIComponent("NSE:" + desk.symbol);

    const item = record();

    const spot =
      item?.analysis?.spot ||
      item?.analysis?.spot_ltp ||
      selectedBars().at(-1)?.close;

    el("desk-spot").textContent = spot ? quantity.format(spot) : "—";

    el("desk-observed").textContent = timestamp(item?.observed_epoch);

    el("desk-source").textContent = item?.source || "No recording";

    const inspectedBar = selectedBars()[desk.bar];

    const historical =
      inspectedBar &&
      item?.events?.find(
        (event) => event.timestamp === inspectedBar.timestamp && event.analysis,
      );

    renderDecision(
      historical
        ? {
            ...item,

            analysis: historical.analysis,

            observed_epoch: historical.observed_epoch,
          }
        : item,
    );

    renderLevels(item);

    renderExecutions();

    renderScanners();

    const signature = JSON.stringify([
      desk.bot,

      desk.symbol,

      item?.candles,

      item?.events,

      item?.levels,
    ]);

    el("structure-empty").hidden = selectedBars().length > 0;

    if (desk.signature !== signature) {
      desk.signature = signature;

      desk.bar = -1;

      const began = performance.now();

      cancelAnimationFrame(desk.frame);

      function animate(now) {
        const progress = reducedMotion.matches
          ? 1
          : Math.min(1, (now - began) / 550);

        drawCandles(1 - (1 - progress) ** 3);

        if (progress < 1) desk.frame = requestAnimationFrame(animate);
      }

      desk.frame = requestAnimationFrame(animate);

      el("candle-readout").textContent =
        "Select a candle to inspect OHLC and its recorded events.";
    }
  }

  function geometry() {
    const canvas = el("structure-chart"),
      bounds = canvas.getBoundingClientRect();

    const bars = selectedBars();

    if (!bounds.width || !bounds.height) return null;

    const dpr = Math.min(devicePixelRatio || 1, 2.5);

    if (
      canvas.width !== Math.round(bounds.width * dpr) ||
      canvas.height !== Math.round(bounds.height * dpr)
    ) {
      canvas.width = Math.round(bounds.width * dpr);

      canvas.height = Math.round(bounds.height * dpr);
    }

    const ctx = canvas.getContext("2d");

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const left = 12,
      right = bounds.width - 66,
      top = 35,
      bottom = bounds.height - 63;

    const low = bars.length ? Math.min(...bars.map((b) => b.low)) : 0;

    const high = bars.length ? Math.max(...bars.map((b) => b.high)) : 1;

    const padding = Math.max((high - low) * 0.1, high * 0.0001);

    return {
      ctx,

      bounds,

      bars,

      left,

      right,

      top,

      bottom,

      min: low - padding,

      max: high + padding,

      x: (i) => left + ((i + 0.5) / Math.max(1, bars.length)) * (right - left),

      y: (price) =>
        bottom -
        ((price - low + padding) / (high - low + 2 * padding)) * (bottom - top),
    };
  }

  function drawCandles(opacity = 1) {
    const g = geometry();

    if (!g) return;

    const { ctx, bounds, bars, left, right, top, bottom, x, y, min, max } = g;

    ctx.clearRect(0, 0, bounds.width, bounds.height);

    candleNavigation.sync();

    if (!bars.length) return;

    ctx.globalAlpha = opacity;

    ctx.font = '9px "IBM Plex Mono",monospace';

    ctx.textBaseline = "middle";

    for (let i = 0; i < 5; i++) {
      const price = min + ((max - min) * i) / 4,
        yy = y(price);

      ctx.setLineDash([2, 5]);

      ctx.strokeStyle = "#a5bac52e";

      ctx.lineWidth = 1;

      ctx.beginPath();

      ctx.moveTo(left, yy);

      ctx.lineTo(right, yy);

      ctx.stroke();

      ctx.fillStyle = "#bdccd4";

      ctx.fillText(quantity.format(price), right + 9, yy);
    }

    ctx.setLineDash([]);

    const mapped = [...(record()?.levels || [])]

      .filter((l) => l.price >= min && l.price <= max)

      .sort(
        (a, b) =>
          Math.abs(a.price - bars.at(-1).close) -
          Math.abs(b.price - bars.at(-1).close),
      )

      .slice(0, 8);

    ctx.save();

    ctx.beginPath();

    ctx.rect(left, top - 10, right - left, bottom - top + 20);

    ctx.clip();

    const labelRows = [];

    for (const level of mapped) {
      const isZone =
        level.kind !== "SUPPORT_RESISTANCE" &&
        ["5m", "15m", "1h", "4h"].includes(level.timeframe);

      if (isZone ? !desk.overlays.zones : !desk.overlays.levels) continue;

      const formedIndex = level.formed_epoch
        ? bars.findIndex((bar) => bar.timestamp >= level.formed_epoch)
        : 0;

      if (formedIndex < 0) continue;

      const zoneLeft = formedIndex > 0 ? x(formedIndex) : left;

      const palette = {
        "5m": "#ff9f1a",

        "15m": "#3385ff",

        "1h": "#9c69ff",

        "4h": "#c74cff",

        Session: "#ffb329",
      };

      const color = isZone
        ? level.role === "SUPPLY"
          ? "#ff851b"
          : "#3385ff"
        : palette[level.timeframe] || "#ffb329";

      ctx.fillStyle = color + "20";

      const upper = y(level.upper),
        lower = y(level.lower);

      ctx.fillRect(
        zoneLeft,

        upper,

        right - zoneLeft,

        Math.max(3, lower - upper),
      );

      ctx.strokeStyle =
        desk.focusedLevel === level.price ? color : color + "dd";

      ctx.lineWidth = desk.focusedLevel === level.price ? 1.8 : 1;

      ctx.setLineDash([5, 5]);

      ctx.beginPath();

      ctx.moveTo(zoneLeft, y(level.price));

      ctx.lineTo(right, y(level.price));

      ctx.stroke();

      ctx.setLineDash([]);

      ctx.fillStyle = color;

      ctx.font = '8px "IBM Plex Mono",monospace';

      if (
        desk.focusedLevel === level.price ||
        labelRows.every((yy) => Math.abs(yy - y(level.price)) >= 15)
      ) {
        labelRows.push(y(level.price));

        ctx.fillText(
          `${level.timeframe} ${level.role === "SUPPLY" ? "R" : "S"} ${quantity.format(level.price)}`,

          left + 5,

          y(level.price) - 7,
        );
      }
    }

    const barWidth = Math.max(
      1,

      Math.min(10, ((right - left) / bars.length) * 0.72),
    );

    const maxVolume = Math.max(1, ...bars.map((b) => b.volume || 0));

    bars.forEach((bar, i) => {
      const breakout =
        desk.overlays.events &&
        (record()?.events || []).some(
          (e) => e.type === "BREAKOUT" && e.timestamp === bar.timestamp,
        );

      const green = bar.close >= bar.open,
        color = breakout ? "#39ff88" : green ? "#00dfaa" : "#ff3153";

      ctx.shadowColor = breakout ? "#39ff88" : "transparent";

      ctx.shadowBlur = breakout ? 16 : 0;

      ctx.strokeStyle = color;

      ctx.lineWidth = 1.1;

      ctx.beginPath();

      ctx.moveTo(x(i), y(bar.high));

      ctx.lineTo(x(i), y(bar.low));

      ctx.stroke();

      ctx.fillStyle = color;

      ctx.fillRect(
        x(i) - barWidth / 2,

        Math.min(y(bar.open), y(bar.close)),

        barWidth,

        Math.max(1, Math.abs(y(bar.close) - y(bar.open))),
      );

      ctx.shadowBlur = 0;

      ctx.fillStyle = green ? "#00dfaa60" : "#ff315360";

      const volumeHeight = ((bar.volume || 0) / maxVolume) * 26;

      ctx.fillRect(
        x(i) - barWidth / 2,

        bottom + 33 - volumeHeight,

        barWidth,

        volumeHeight,
      );
    });

    ctx.restore();

    if (desk.overlays.events) {
      const seen = new Set();

      (record()?.events || []).forEach((event, i) => {
        const index = bars.findIndex(
          (bar) => bar.timestamp === event.timestamp,
        );

        const key = event.timestamp + ":" + event.type;

        if (index < 0 || seen.has(key)) return;

        seen.add(key);

        const xx = x(index),
          color =
            event.type === "SWEEP"
              ? "#ceb7ec"
              : event.side === "PE"
                ? "#e4a1ad"
                : "#b6efd6";

        ctx.setLineDash([2, 4]);

        ctx.strokeStyle = color + "55";

        ctx.beginPath();

        ctx.moveTo(xx, top);

        ctx.lineTo(xx, bottom);

        ctx.stroke();

        ctx.setLineDash([]);

        ctx.beginPath();

        ctx.arc(xx, y(bars[index].high) - 7, 3, 0, Math.PI * 2);

        ctx.fillStyle = color;

        ctx.fill();

        ctx.font = '8px "IBM Plex Mono",monospace';

        ctx.fillText(
          event.type + " " + candleTime(event.timestamp),

          Math.min(right - 88, Math.max(left, xx - 15)),

          12 + (i % 2) * 13,
        );
      });
    }

    ctx.font = '8px "IBM Plex Mono",monospace';

    ctx.fillStyle = "#7c9587";

    [
      0,

      Math.floor(bars.length / 3),

      Math.floor((bars.length * 2) / 3),

      bars.length - 1,
    ].forEach((i) =>
      ctx.fillText(
        candleTime(bars[i].timestamp),

        Math.min(right - 25, Math.max(left, x(i) - 12)),

        bounds.height - 13,
      ),
    );

    if (desk.bar >= 0 && bars[desk.bar]) {
      const bar = bars[desk.bar],
        xx = x(desk.bar);

      ctx.strokeStyle = "#e4f5eb55";

      ctx.setLineDash([3, 4]);

      ctx.beginPath();

      ctx.moveTo(xx, top);

      ctx.lineTo(xx, bottom + 36);

      ctx.stroke();

      ctx.setLineDash([]);

      ctx.fillStyle = "#e5f6ec";

      ctx.beginPath();

      ctx.arc(xx, y(bar.close), 3, 0, Math.PI * 2);

      ctx.fill();
    }

    const positions = (state.data?.positions || []).filter(
      (p) => p.segment === desk.bot && p.symbol === desk.symbol,
    );

    positions.slice(0, 2).forEach((position, index) => {
      const boxWidth = Math.min(240, right - left - 12),
        bx = right - boxWidth - 5,
        by = top + 18 + index * 116;

      const entry = Number(position.entry),
        stop = Number(position.stop),
        target = Number(position.target);

      ctx.fillStyle = "#050b10ee";
      ctx.fillRect(bx, by, boxWidth, 108);

      ctx.fillStyle = "#00dfaa25";
      ctx.fillRect(bx + 5, by + 24, boxWidth - 10, 42);

      ctx.fillStyle = "#ff315328";
      ctx.fillRect(bx + 5, by + 66, boxWidth - 10, 31);

      ctx.font = '9px "IBM Plex Mono",monospace';
      ctx.fillStyle = "#f4fff9";

      ctx.fillText("LONG OPTION · PREMIUM (INR)", bx + 10, by + 13);

      ctx.fillStyle = "#39ff88";
      ctx.fillText(
        Number.isFinite(target) && target > 0
          ? "Target " + money(target)
          : "Target not recorded",
        bx + 10,
        by + 39,
      );

      ctx.strokeStyle = "#ffffff";
      ctx.beginPath();
      ctx.moveTo(bx + 5, by + 66);
      ctx.lineTo(bx + boxWidth - 5, by + 66);
      ctx.stroke();

      ctx.fillStyle = "#ffffff";
      ctx.fillText("Entry " + money(entry), bx + 10, by + 59);

      ctx.fillStyle = "#ff6681";
      ctx.fillText("SL " + money(stop), bx + 10, by + 84);

      ctx.fillStyle = "#b9ced8";
      ctx.fillText(
        String(position.contract || "Saved PAPER position").slice(0, 34),
        bx + 10,
        by + 102,
      );
    });

    ctx.globalAlpha = 1;
  }

  function inspect(index) {
    const bars = selectedBars();

    if (!bars.length) return;

    const nextBar = Math.max(0, Math.min(bars.length - 1, index));

    if (desk.bar === nextBar) return;

    desk.bar = nextBar;

    const bar = bars[desk.bar];

    const events = (record()?.events || []).filter(
      (e) => e.timestamp === bar.timestamp,
    );

    const historical = events.find((event) => event.analysis);

    renderDecision(
      historical
        ? {
            ...record(),

            analysis: historical.analysis,

            observed_epoch: historical.observed_epoch,
          }
        : record(),
    );

    el("candle-readout").textContent =
      `${timestamp(bar.timestamp)}–${candleTime(bar.timestamp + 300)} · O ${quantity.format(bar.open)} H ${quantity.format(bar.high)} L ${quantity.format(bar.low)} C ${quantity.format(bar.close)}${events.length ? " · " + events.map((e) => e.type + " " + e.detail).join(" / ") : ""}`;

    drawCandles();
  }

  let pointerFrame = null;

  el("structure-chart").addEventListener(
    "pointermove",

    (event) => {
      const g = geometry();

      if (!g || !g.bars.length || candleNavigation.busy()) return;

      const i = Math.floor(
        ((event.clientX - g.bounds.left - g.left) / (g.right - g.left)) *
          g.bars.length,
      );

      if (pointerFrame) cancelAnimationFrame(pointerFrame);

      pointerFrame = requestAnimationFrame(() => {
        pointerFrame = null;

        inspect(i);
      });
    },

    { passive: true },
  );

  el("structure-chart").addEventListener("keydown", (event) => {
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();

      inspect(event.key === "Home" ? 0 : selectedBars().length - 1);

      return;
    }

    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();

      inspect(
        (desk.bar < 0 ? selectedBars().length - 1 : desk.bar) +
          (event.key === "ArrowRight" ? 1 : -1),
      );
    }
  });

  new ResizeObserver(() => drawCandles()).observe(el("structure-chart"));

  el("desk-symbol").addEventListener("change", (event) =>
    selectBot(desk.bot, event.target.value),
  );

  document

    .querySelectorAll("[data-bot]")

    .forEach((button) =>
      button.addEventListener("click", () => selectBot(button.dataset.bot)),
    );

  document.querySelectorAll("[data-overlay]").forEach((button) =>
    button.addEventListener("click", () => {
      desk.overlays[button.dataset.overlay] =
        !desk.overlays[button.dataset.overlay];

      button.classList.toggle(
        "selected",

        desk.overlays[button.dataset.overlay],
      );

      button.setAttribute(
        "aria-pressed",

        String(desk.overlays[button.dataset.overlay]),
      );

      drawCandles();
    }),
  );

  document.querySelectorAll("[data-period]").forEach((button) =>
    button.addEventListener("click", () => {
      renderBotCards();

      renderExecutions();
    }),
  );

  document.querySelectorAll("[data-page]").forEach((button) =>
    button.addEventListener("click", () => {
      el("bot-workspace").hidden = button.dataset.page !== "overview";
    }),
  );

  async function refreshDesk() {
    if (desk.loading || document.hidden) return;

    desk.loading = true;

    try {
      const response = await fetch("/api/desk", { cache: "no-store" });

      if (!response.ok) throw new Error("Unavailable");

      desk.data = await response.json();

      renderWorkspace();
    } catch (error) {
      el("desk-observed").textContent =
        "Observation feed unavailable · retrying";
    } finally {
      desk.loading = false;
    }
  }

  el("refresh").addEventListener("click", refreshDesk);

  setInterval(refreshDesk, 3000);

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshDesk();
  });

  refreshDesk();
})();
