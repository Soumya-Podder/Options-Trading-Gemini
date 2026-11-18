/* Shared pointer, wheel, pinch and button navigation for both canvas charts. */
function createChartNavigation(canvas, options) {
  let count = options.initialCount ?? null;
  let end = null;
  let drawFrame = null;
  let drag = null;
  let pinch = null;
  let moved = false;
  const pointers = new Map();
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

  function range() {
    const length = options.length();
    const size = Math.min(
      length,
      Math.max(options.minCount || 2, count ?? length),
    );
    const finish = clamp(Math.round(end ?? length), size, length);
    return { start: finish - size, end: finish, size, length };
  }
  function changed() {
    if (drawFrame) return;
    drawFrame = requestAnimationFrame(() => {
      drawFrame = null;
      options.onChange();
    });
  }
  function resize(next, anchor = 0.5) {
    const old = range();
    if (!old.length) return;
    count = clamp(
      Math.round(next),
      Math.min(options.minCount || 2, old.length),
      old.length,
    );
    end = clamp(
      old.start + old.size * anchor + count * (1 - anchor),
      count,
      old.length,
    );
    changed();
  }
  function zoom(factor, anchor = 0.5) {
    resize(range().size * factor, anchor);
  }
  function reset() {
    count = options.initialCount ?? null;
    end = null;
    changed();
  }
  function plot() {
    const bounds = canvas.getBoundingClientRect();
    const area = options.plot(bounds.width);
    return { ...area, bounds, width: area.right - area.left };
  }
  function anchor(clientX) {
    const area = plot();
    return clamp((clientX - area.bounds.left - area.left) / area.width, 0, 1);
  }
  canvas.addEventListener(
    "wheel",
    (event) => {
      if (range().length < 2 || !event.deltaY) return;
      event.preventDefault();
      zoom(event.deltaY < 0 ? 0.8 : 1.25, anchor(event.clientX));
    },
    { passive: false },
  );
  canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 && event.pointerType !== "touch") return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    canvas.setPointerCapture(event.pointerId);
    canvas.focus({ preventScroll: true });
    moved = false;
    if (pointers.size === 1)
      drag = { x: event.clientX, end: range().end, size: range().size };
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = {
        distance: Math.hypot(a.x - b.x, a.y - b.y),
        size: range().size,
      };
      drag = null;
    }
    canvas.classList.add("chart-grabbing");
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 2 && pinch) {
      const [a, b] = [...pointers.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      if (distance > 5 && pinch.distance > 5) {
        moved = true;
        resize(
          (pinch.size * pinch.distance) / distance,
          anchor((a.x + b.x) / 2),
        );
      }
    } else if (drag && Math.abs(event.clientX - drag.x) > 2) {
      moved = true;
      const current = range();
      end = clamp(
        drag.end +
          ((drag.x - event.clientX) / Math.max(1, plot().width)) * drag.size,
        current.size,
        current.length,
      );
      changed();
    }
  });
  function release(event) {
    pointers.delete(event.pointerId);
    drag = null;
    pinch = null;
    if (!pointers.size) canvas.classList.remove("chart-grabbing");
  }
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", release);
  canvas.addEventListener("lostpointercapture", release);
  canvas.addEventListener("dblclick", reset);
  canvas.addEventListener("keydown", (event) => {
    if (["+", "=", "-", "0"].includes(event.key)) {
      event.preventDefault();
      if (event.key === "0") reset();
      else zoom(event.key === "-" ? 1.25 : 0.8);
    }
  });
  const inside = document.getElementById(options.controls);
  const zoomIn = inside.querySelector('[data-zoom="in"]');
  const zoomOut = inside.querySelector('[data-zoom="out"]');
  const label = inside.querySelector("[data-chart-range]");
  zoomIn.addEventListener("click", () => zoom(0.8));
  zoomOut.addEventListener("click", () => zoom(1.25));
  inside.querySelector('[data-zoom="reset"]').addEventListener("click", reset);
  return {
    range,
    reveal(index) {
      if (index < 0) return;
      const current = range();
      end = clamp(
        index + Math.ceil(current.size / 2),
        current.size,
        current.length,
      );
      changed();
    },
    reset,
    busy: () => pointers.size > 1 || (pointers.size > 0 && moved),
    sync() {
      const current = range();
      zoomIn.disabled =
        current.size <= Math.min(options.minCount || 2, current.length);
      zoomOut.disabled = current.size >= current.length;
      const text = current.length
        ? `${current.size} / ${current.length} ${options.unit}`
        : "No recorded data";
      if (label.textContent !== text) label.textContent = text;
    },
  };
}
