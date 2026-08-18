(function () {
  "use strict";

  const root = document.getElementById("root");
  const NS = "http://www.w3.org/2000/svg";
  const DEFAULT_SUBS = [
    { code: "EC1", name: "Lineaboard Cladding", color: "#a9bfd9" },
    { code: "EC2", name: "Textureboard Cladding", color: "#a5b9db" },
    { code: "EC3", name: "Easylap Cladding", color: "#b7c4d5" },
    { code: "RBL", name: "Rendered Block", color: "#b9c0c4" },
    { code: "SOF", name: "Soffits / Eaves", color: "#d9c788" },
    { code: "EC5", name: "Timber Look Cladding", color: "#a77c52" },
    { code: "SCR", name: "Aluminium Screens", color: "#bea8b5" },
    { code: "BA1", name: "Glass Balustrade", color: "#b9c7c8" },
    { code: "SHD", name: "Sunhoods", color: "#919b8d" },
    { code: "BC", name: "Cappings & Gutters", color: "#9aa7aa" },
    { code: "RS", name: "Roof Sheet", color: "#c45f74" },
    { code: "DP", name: "Downpipes", color: "#84a77e" },
    { code: "GD", name: "Garage Doors", color: "#7e8792" }
  ];

  const state = {
    image: "",
    areas: [],
    subs: DEFAULT_SUBS,
    pxPerM: 0,
    pageType: "",
    viewLabel: "",
    revision: -1,
    selected: null,
    mode: "select",
    drag: null,
    draft: [],
    draftRect: null,
    natW: 0,
    natH: 0,
    xray: false,
    showSoffits: true,
    history: [],
    future: []
  };

  function post(type, payload) {
    window.parent.postMessage(Object.assign({ isStreamlitMessage: true, type: type }, payload || {}), "*");
  }

  function n(value, fallback) {
    const parsed = parseFloat(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }

  function deep(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function point(value) {
    return {
      x: clamp(n(value && value.x, 0), 0, 100),
      y: clamp(n(value && value.y, 0), 0, 100)
    };
  }

  function boxPoints(raw) {
    const x = clamp(n(raw.x, 15), 0, 100);
    const y = clamp(n(raw.y, 15), 0, 100);
    const width = Math.max(1, n(raw.w, 20));
    const height = Math.max(1, n(raw.h, 12));
    const x2 = clamp(x + width, 0, 100);
    const y2 = clamp(y + height, 0, 100);
    return [{ x: x, y: y }, { x: x2, y: y }, { x: x2, y: y2 }, { x: x, y: y2 }];
  }

  function nextId() {
    let max = 0;
    state.areas.forEach((area) => {
      const match = String(area.id || "").match(/(\d+)/);
      if (match) max = Math.max(max, parseInt(match[1], 10) || 0);
    });
    return "A-" + String(max + 1).padStart(3, "0");
  }

  function normalizeArea(raw, index) {
    const points = Array.isArray(raw.points) && raw.points.length >= 3 ? raw.points.map(point) : boxPoints(raw || {});
    return {
      id: String(raw.id || ("A-" + String(index + 1).padStart(3, "0"))),
      label: String(raw.label || raw.id || ("Area " + (index + 1))),
      substrate: String(raw.substrate || (state.subs[0] && state.subs[0].code) || "OTHER"),
      elevation: String(raw.elevation || state.viewLabel || ""),
      status: String(raw.status || "Paint Included"),
      progress_pct: clamp(n(raw.progress_pct, 0), 0, 100),
      notes: String(raw.notes || ""),
      manual_m2: Math.max(0, n(raw.manual_m2, 0)),
      points: points
    };
  }

  function serializedAreas() {
    return state.areas.map((area, index) => normalizeArea(area, index));
  }

  function pushHistory() {
    state.history.push(deep(serializedAreas()));
    if (state.history.length > 40) state.history.shift();
    state.future = [];
  }

  function restore(items) {
    state.areas = (items || []).map((item, index) => normalizeArea(item, index));
    if (state.selected && !state.areas.some((area) => area.id === state.selected)) {
      state.selected = state.areas.length ? state.areas[0].id : null;
    }
    emitValue();
    render();
  }

  function undo() {
    if (!state.history.length) return;
    state.future.push(deep(serializedAreas()));
    restore(state.history.pop());
  }

  function redo() {
    if (!state.future.length) return;
    state.history.push(deep(serializedAreas()));
    restore(state.future.pop());
  }

  function emitValue() {
    state.areas = serializedAreas();
    post("streamlit:setComponentValue", { value: { areas: state.areas }, dataType: "json" });
  }

  function substrateFor(code) {
    return state.subs.find((item) => String(item.code) === String(code)) || {
      code: code || "OTHER",
      name: code || "Other",
      color: "#80a6c9"
    };
  }

  function rgba(hex, alpha) {
    let value = String(hex || "#80a6c9").replace("#", "");
    if (value.length === 3) value = value.split("").map((part) => part + part).join("");
    const parsed = parseInt(value, 16);
    if (!Number.isFinite(parsed)) return "rgba(80,166,201," + alpha + ")";
    return "rgba(" + ((parsed >> 16) & 255) + "," + ((parsed >> 8) & 255) + "," + (parsed & 255) + "," + alpha + ")";
  }

  function polygonPixelArea(points) {
    if (!state.natW || !state.natH || !Array.isArray(points) || points.length < 3) return 0;
    let twice = 0;
    for (let index = 0; index < points.length; index += 1) {
      const a = points[index];
      const b = points[(index + 1) % points.length];
      const ax = a.x / 100 * state.natW;
      const ay = a.y / 100 * state.natH;
      const bx = b.x / 100 * state.natW;
      const by = b.y / 100 * state.natH;
      twice += ax * by - bx * ay;
    }
    return Math.abs(twice) / 2;
  }

  function areaM2(area) {
    if (n(area.manual_m2, 0) > 0) return n(area.manual_m2, 0);
    return state.pxPerM > 0 ? polygonPixelArea(area.points) / (state.pxPerM * state.pxPerM) : 0;
  }

  function summary() {
    let total = 0;
    let completed = 0;
    state.areas.forEach((area) => {
      if (area.status === "Excluded") return;
      const measured = areaM2(area);
      total += measured;
      completed += measured * clamp(n(area.progress_pct, 0), 0, 100) / 100;
    });
    return {
      total: total,
      completed: completed,
      remaining: Math.max(0, total - completed),
      pct: total > 0 ? completed / total * 100 : 0
    };
  }

  function centroid(points) {
    if (!points.length) return { x: 0, y: 0 };
    let x = 0;
    let y = 0;
    points.forEach((item) => {
      x += item.x;
      y += item.y;
    });
    return { x: x / points.length, y: y / points.length };
  }

  function selectedArea() {
    return state.areas.find((area) => area.id === state.selected) || null;
  }

  function pointerPosition(event) {
    const image = document.getElementById("bg");
    if (!image) return { x: 0, y: 0 };
    const rect = image.getBoundingClientRect();
    return {
      x: clamp((event.clientX - rect.left) / rect.width * 100, 0, 100),
      y: clamp((event.clientY - rect.top) / rect.height * 100, 0, 100)
    };
  }

  function svgElement(name, attrs) {
    const element = document.createElementNS(NS, name);
    Object.keys(attrs || {}).forEach((key) => element.setAttribute(key, String(attrs[key])));
    return element;
  }

  function pointsAttribute(points) {
    return points.map((item) => item.x + "," + item.y).join(" ");
  }

  function areaVisible(area) {
    return state.showSoffits || String(area.substrate).toUpperCase() !== "SOF";
  }

  function modeHint() {
    if (state.mode === "box") return "<strong>Draw box mode</strong>Click and drag over any area.";
    if (state.mode === "poly") return "<strong>Draw polygon mode</strong>Click each corner; double-click or press Finish.";
    return "<strong>Select mode</strong>Select an area, move it, or drag its blue corner points.";
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (match) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\"": "&quot;",
      "'": "&#39;"
    }[match]));
  }

  function attr(value) {
    return escapeHtml(value);
  }

  function fmt(value, digits) {
    const places = digits === undefined ? 2 : digits;
    return n(value, 0).toLocaleString(undefined, { minimumFractionDigits: places, maximumFractionDigits: places });
  }

  function selectedOption(actual, expected) {
    return actual === expected ? "selected" : "";
  }

  function render() {
    const totals = summary();
    root.innerHTML =
      '<div class="studio">' +
        '<div class="header"><div class="brand"><span class="brandmark">PB</span><span>PlanReader Takeoff Studio</span></div>' +
          '<div class="headmeta"><span class="pill active">TAKEOFF STUDIO</span><span class="pill">' + escapeHtml(state.pageType || "Drawing") + '</span><span class="pill">' + escapeHtml(state.viewLabel || "Current view") + '</span></div></div>' +
        '<div class="bodygrid"><aside class="side" id="left"></aside>' +
          '<main class="canvas-col"><div class="canvas-toolbar"><span class="strong">' + escapeHtml(state.viewLabel || "Drawing view") + '</span><span>·</span><span>' + escapeHtml(state.pageType || "") + '</span><span style="margin-left:auto" id="scaleTxt"></span></div>' +
          '<div class="canvas-wrap" id="canvasWrap"></div><div class="stats" id="stats"></div></main>' +
          '<aside class="side right" id="right"></aside></div>' +
        '<div class="footer">Measurements from calibrated elevations are scale-based. Areas drawn on perspective renders are visual/provisional unless manually verified.</div>' +
      '</div>';
    renderLeft();
    renderStage();
    renderRight();
    renderStats();
    document.getElementById("scaleTxt").textContent = state.pxPerM > 0 ? fmt(state.pxPerM) + " px/m · calibrated" : "Scale not calibrated";
    resizeFrame();
  }

  function renderStats() {
    const holder = document.getElementById("stats");
    if (!holder) return;
    const totals = summary();
    holder.innerHTML =
      '<div class="stat"><div class="modehint">' + modeHint() + '</div></div>' +
      '<div class="stat"><div class="k">Total Areas</div><div class="v">' + fmt(totals.total) + ' m²</div></div>' +
      '<div class="stat"><div class="k">Completed</div><div class="v green">' + fmt(totals.completed) + ' m² (' + fmt(totals.pct, 1) + '%)</div></div>' +
      '<div class="stat"><div class="k">Remaining</div><div class="v orange">' + fmt(totals.remaining) + ' m²</div></div>';
  }

  function renderLeft() {
    const left = document.getElementById("left");
    let legend = '<div class="section"><div class="section-title">Substrate legend</div><div class="legend-list">';
    state.subs.forEach((item) => {
      legend += '<div class="legend-row" data-sub="' + attr(item.code) + '"><span class="swatch" style="background:' + attr(item.color) + '"></span><span class="legend-code">' + escapeHtml(item.code) + '</span><span class="legend-name">' + escapeHtml(item.name) + '</span></div>';
    });
    legend += "</div></div>";
    left.innerHTML = legend +
      '<div class="section"><div class="section-title">Tools</div><div class="toolgrid">' +
        '<button class="tool ' + (state.mode === "select" ? "active" : "") + '" id="selectTool"><span class="ico">⌖</span>Select</button>' +
        '<button class="tool ' + (state.mode === "box" ? "active" : "") + '" id="boxTool"><span class="ico">▧</span>Draw Box</button>' +
        '<button class="tool ' + (state.mode === "poly" ? "active" : "") + '" id="polyTool"><span class="ico">⬠</span>Polygon</button>' +
        '<button class="tool" id="undoTool" ' + (!state.history.length ? "disabled" : "") + '><span class="ico">↶</span>Undo</button>' +
        '<button class="tool" id="redoTool" ' + (!state.future.length ? "disabled" : "") + '><span class="ico">↷</span>Redo</button>' +
        '<button class="tool danger" id="clearTool"><span class="ico">⌫</span>Clear All</button>' +
      '</div>' + (state.mode === "poly" && state.draft.length ? '<button class="action blue" id="finishPoly">Finish polygon</button>' : '') + '</div>' +
      '<div class="section"><div class="section-title">View options</div>' +
        '<label class="radio"><input type="radio" name="viewmode" value="real" ' + (!state.xray ? "checked" : "") + '> Realistic</label>' +
        '<label class="radio"><input type="radio" name="viewmode" value="xray" ' + (state.xray ? "checked" : "") + '> X-Ray / Transparent</label>' +
        '<label class="toggle"><input type="checkbox" id="showSoffits" ' + (state.showSoffits ? "checked" : "") + '> Show soffits</label></div>' +
      '<div class="section"><div class="section-title">Model views</div>' +
        '<div class="viewitem active">▧ ' + escapeHtml(state.viewLabel || "Current drawing") + '</div>' +
        '<div class="viewitem">◫ Elevation overlay</div><div class="viewitem">⌂ 3D model in PlanReader tab</div></div>';

    left.querySelectorAll(".legend-row").forEach((element) => element.addEventListener("click", () => {
      const area = selectedArea();
      if (!area) return;
      pushHistory();
      area.substrate = element.dataset.sub;
      emitValue();
      render();
    }));
    left.querySelector("#selectTool").onclick = () => { state.mode = "select"; state.draft = []; state.draftRect = null; render(); };
    left.querySelector("#boxTool").onclick = () => { state.mode = "box"; state.draft = []; state.draftRect = null; render(); };
    left.querySelector("#polyTool").onclick = () => { state.mode = "poly"; state.draft = []; state.draftRect = null; render(); };
    left.querySelector("#undoTool").onclick = undo;
    left.querySelector("#redoTool").onclick = redo;
    left.querySelector("#clearTool").onclick = () => {
      if (state.areas.length && window.confirm("Clear all Takeoff Studio areas on this drawing?")) {
        pushHistory();
        state.areas = [];
        state.selected = null;
        emitValue();
        render();
      }
    };
    const finish = left.querySelector("#finishPoly");
    if (finish) finish.onclick = finishPolygon;
    left.querySelectorAll('input[name="viewmode"]').forEach((radio) => {
      radio.onchange = () => { state.xray = radio.value === "xray"; render(); };
    });
    left.querySelector("#showSoffits").onchange = (event) => { state.showSoffits = !!event.target.checked; render(); };
  }

  function renderStage() {
    const wrap = document.getElementById("canvasWrap");
    if (!state.image) {
      wrap.innerHTML = '<div class="empty">No rendered drawing image is available for this page.</div>';
      return;
    }
    const stage = document.createElement("div");
    stage.className = "stage" + (state.xray ? " xray" : "");
    stage.id = "stage";
    const image = document.createElement("img");
    image.id = "bg";
    image.src = state.image;
    image.alt = "PlanReader drawing";
    stage.appendChild(image);
    const svg = svgElement("svg", { class: "overlay", viewBox: "0 0 100 100", preserveAspectRatio: "none" });
    svg.id = "overlay";
    stage.appendChild(svg);
    wrap.appendChild(stage);
    image.onload = () => {
      state.natW = image.naturalWidth;
      state.natH = image.naturalHeight;
      drawOverlay();
      resizeFrame();
    };
    if (image.complete && image.naturalWidth) {
      state.natW = image.naturalWidth;
      state.natH = image.naturalHeight;
      drawOverlay();
    }
    bindCanvas(svg);
  }

  function drawOverlay() {
    const svg = document.getElementById("overlay");
    const stage = document.getElementById("stage");
    if (!svg || !stage) return;
    svg.innerHTML = "";
    stage.querySelectorAll(".chip").forEach((element) => element.remove());

    state.areas.forEach((area) => {
      const substrate = substrateFor(area.substrate);
      const selected = area.id === state.selected;
      const polygon = svgElement("polygon", {
        points: pointsAttribute(area.points),
        class: "area" + (selected ? " selected" : "") + (areaVisible(area) ? "" : " hidden"),
        fill: rgba(substrate.color, selected ? 0.30 : 0.17),
        stroke: substrate.color
      });
      polygon.dataset.id = area.id;
      polygon.addEventListener("pointerdown", (event) => {
        if (state.mode !== "select") return;
        event.preventDefault();
        event.stopPropagation();
        state.selected = area.id;
        pushHistory();
        const where = pointerPosition(event);
        state.drag = { kind: "move", id: area.id, start: where, points: deep(area.points) };
        drawOverlay();
        renderRight();
      });
      svg.appendChild(polygon);

      if (selected && areaVisible(area)) {
        area.points.forEach((item, index) => {
          const vertex = svgElement("circle", { cx: item.x, cy: item.y, r: 0.85, class: "vertex" });
          vertex.addEventListener("pointerdown", (event) => {
            if (state.mode !== "select") return;
            event.preventDefault();
            event.stopPropagation();
            pushHistory();
            state.drag = { kind: "vertex", id: area.id, index: index };
          });
          svg.appendChild(vertex);
          const following = area.points[(index + 1) % area.points.length];
          const middle = { x: (item.x + following.x) / 2, y: (item.y + following.y) / 2 };
          const midpoint = svgElement("circle", { cx: middle.x, cy: middle.y, r: 0.48, class: "mid" });
          midpoint.addEventListener("pointerdown", (event) => {
            if (state.mode !== "select") return;
            event.preventDefault();
            event.stopPropagation();
            pushHistory();
            area.points.splice(index + 1, 0, point(middle));
            emitValue();
            drawOverlay();
            renderRight();
            renderStats();
          });
          svg.appendChild(midpoint);
        });
      }

      if (areaVisible(area)) {
        const center = centroid(area.points);
        const chip = document.createElement("div");
        chip.className = "chip";
        chip.style.left = center.x + "%";
        chip.style.top = center.y + "%";
        chip.innerHTML = "<b>" + escapeHtml(area.substrate || "AREA") + "</b><br>" + fmt(areaM2(area)) + " m²";
        stage.appendChild(chip);
      }
    });

    if (state.mode === "poly" && state.draft.length) {
      svg.appendChild(svgElement("polyline", { points: pointsAttribute(state.draft), class: "draft", fill: "none" }));
      state.draft.forEach((item) => svg.appendChild(svgElement("circle", { cx: item.x, cy: item.y, r: 0.75, class: "vertex" })));
    }
    if (state.mode === "box" && state.draftRect) {
      const rect = state.draftRect;
      const points = [rect.a, { x: rect.b.x, y: rect.a.y }, rect.b, { x: rect.a.x, y: rect.b.y }];
      svg.appendChild(svgElement("polygon", { points: pointsAttribute(points), class: "draft" }));
    }
  }

  function bindCanvas(svg) {
    svg.addEventListener("pointerdown", (event) => {
      if (event.target !== svg) return;
      const where = pointerPosition(event);
      if (state.mode === "box") {
        event.preventDefault();
        state.drag = { kind: "box", start: where };
        state.draftRect = { a: where, b: where };
        drawOverlay();
      } else if (state.mode === "poly") {
        event.preventDefault();
        state.draft.push(point(where));
        drawOverlay();
        renderLeft();
        renderStats();
      } else {
        state.selected = null;
        drawOverlay();
        renderRight();
      }
    });
    svg.addEventListener("dblclick", (event) => {
      if (state.mode === "poly") {
        event.preventDefault();
        finishPolygon();
      }
    });
  }

  function handlePointerMove(event) {
    if (!state.drag) return;
    const where = pointerPosition(event);
    if (state.drag.kind === "box") {
      state.draftRect = { a: state.drag.start, b: where };
      drawOverlay();
      return;
    }
    const area = state.areas.find((item) => item.id === state.drag.id);
    if (!area) return;
    if (state.drag.kind === "vertex") {
      area.points[state.drag.index] = point(where);
    } else if (state.drag.kind === "move") {
      const dx = where.x - state.drag.start.x;
      const dy = where.y - state.drag.start.y;
      area.points = state.drag.points.map((item) => ({ x: clamp(item.x + dx, 0, 100), y: clamp(item.y + dy, 0, 100) }));
    }
    drawOverlay();
    renderRight();
    renderStats();
  }

  function handlePointerUp() {
    if (!state.drag) return;
    if (state.drag.kind === "box") {
      const rect = state.draftRect;
      state.drag = null;
      state.draftRect = null;
      if (rect && Math.abs(rect.b.x - rect.a.x) > 1 && Math.abs(rect.b.y - rect.a.y) > 1) {
        pushHistory();
        const x1 = Math.min(rect.a.x, rect.b.x);
        const x2 = Math.max(rect.a.x, rect.b.x);
        const y1 = Math.min(rect.a.y, rect.b.y);
        const y2 = Math.max(rect.a.y, rect.b.y);
        addArea([{ x: x1, y: y1 }, { x: x2, y: y1 }, { x: x2, y: y2 }, { x: x1, y: y2 }]);
      } else {
        render();
      }
      return;
    }
    state.drag = null;
    emitValue();
    drawOverlay();
    renderRight();
    renderStats();
  }

  function addArea(points) {
    const id = nextId();
    const area = normalizeArea({
      id: id,
      label: id,
      points: points,
      substrate: (state.subs[0] && state.subs[0].code) || "OTHER",
      elevation: state.viewLabel,
      status: "Paint Included",
      progress_pct: 0
    }, state.areas.length);
    state.areas.push(area);
    state.selected = id;
    emitValue();
    state.mode = "select";
    render();
  }

  function finishPolygon() {
    if (state.draft.length < 3) return;
    pushHistory();
    const points = deep(state.draft);
    state.draft = [];
    addArea(points);
  }

  function renderRight() {
    const right = document.getElementById("right");
    if (!right) return;
    const area = selectedArea();
    if (!area) {
      right.innerHTML = '<div class="section"><div class="section-title">Selected area</div><div class="empty">Select an existing area or use <b>Draw Box</b> / <b>Polygon</b> to create one.</div></div>' +
        '<div class="section"><div class="section-title">Export</div><div class="exportbar"><button id="csvBtn">Export CSV</button><button id="pngBtn">Download Image</button></div></div>';
      right.querySelector("#csvBtn").onclick = downloadCsv;
      right.querySelector("#pngBtn").onclick = downloadImage;
      return;
    }

    const measured = areaM2(area);
    const completed = measured * area.progress_pct / 100;
    const remaining = Math.max(0, measured - completed);
    let options = "";
    state.subs.forEach((item) => {
      options += '<option value="' + attr(item.code) + '" ' + (item.code === area.substrate ? "selected" : "") + '>' + escapeHtml(item.code + " " + item.name) + '</option>';
    });

    right.innerHTML =
      '<div class="section"><div class="section-title">Selected area</div><div class="kv"><span>ID</span><b>' + escapeHtml(area.id) + '</b></div></div>' +
      '<div class="rightcontent">' +
        '<div class="field"><label>Area label</label><input id="label" value="' + attr(area.label) + '"></div>' +
        '<div class="field"><label>Substrate</label><select id="substrate">' + options + '</select></div>' +
        '<div class="field"><label>Elevation / View</label><input id="elev" value="' + attr(area.elevation || state.viewLabel) + '"></div>' +
        '<div class="field"><label>Area (m²)</label><div class="area-readout"><span class="big" id="areaRead">' + (measured > 0 ? fmt(measured) : "To measure") + '</span><span class="unit">m²</span></div></div>' +
        '<div class="field"><label>Manual m² override (optional)</label><input id="manual" type="number" min="0" step="0.01" value="' + (area.manual_m2 > 0 ? attr(area.manual_m2) : "") + '"></div>' +
        '<div class="field"><label>Status</label><select id="status"><option ' + selectedOption(area.status, "Paint Included") + '>Paint Included</option><option ' + selectedOption(area.status, "Separate Item") + '>Separate Item</option><option ' + selectedOption(area.status, "Provisional") + '>Provisional</option><option ' + selectedOption(area.status, "Excluded") + '>Excluded</option></select></div>' +
        '<div class="field"><label>Progress</label><div class="progressrow"><span>Completion</span><span class="progressval" id="progressVal">' + fmt(area.progress_pct, 0) + ' %</span></div><input class="range" id="progress" type="range" min="0" max="100" step="1" value="' + attr(area.progress_pct) + '"><div class="kv"><span>Completed</span><span class="done" id="doneVal">' + fmt(completed) + ' m²</span></div><div class="kv"><span>Remaining</span><span class="remain" id="remainVal">' + fmt(remaining) + ' m²</span></div></div>' +
        '<div class="field"><label>Notes</label><textarea id="notes">' + escapeHtml(area.notes) + '</textarea></div>' +
        '<button class="action blue" id="update">Update area</button><button class="action red" id="delete">Delete area</button>' +
      '</div>' +
      '<div class="section"><div class="section-title">Export</div><div class="exportbar"><button id="csvBtn">Export CSV</button><button id="pngBtn">Download Image</button></div></div>';

    right.querySelector("#update").onclick = () => {
      pushHistory();
      area.label = right.querySelector("#label").value;
      area.substrate = right.querySelector("#substrate").value;
      area.elevation = right.querySelector("#elev").value;
      area.manual_m2 = Math.max(0, n(right.querySelector("#manual").value, 0));
      area.status = right.querySelector("#status").value;
      area.progress_pct = clamp(n(right.querySelector("#progress").value, 0), 0, 100);
      area.notes = right.querySelector("#notes").value;
      emitValue();
      render();
    };

    const progress = right.querySelector("#progress");
    progress.oninput = (event) => {
      area.progress_pct = clamp(n(event.target.value, 0), 0, 100);
      const currentArea = areaM2(area);
      const currentDone = currentArea * area.progress_pct / 100;
      right.querySelector("#progressVal").textContent = fmt(area.progress_pct, 0) + " %";
      right.querySelector("#doneVal").textContent = fmt(currentDone) + " m²";
      right.querySelector("#remainVal").textContent = fmt(Math.max(0, currentArea - currentDone)) + " m²";
      renderStats();
    };
    progress.onchange = () => emitValue();

    right.querySelector("#delete").onclick = () => {
      pushHistory();
      state.areas = state.areas.filter((item) => item.id !== area.id);
      state.selected = state.areas.length ? state.areas[0].id : null;
      emitValue();
      render();
    };
    right.querySelector("#csvBtn").onclick = downloadCsv;
    right.querySelector("#pngBtn").onclick = downloadImage;
  }

  function downloadCsv() {
    const rows = [["ID", "Label", "Substrate", "Elevation", "Area m2", "Status", "Progress %", "Completed m2", "Remaining m2", "Notes"]];
    state.areas.forEach((area) => {
      const measured = areaM2(area);
      const done = measured * area.progress_pct / 100;
      rows.push([area.id, area.label, area.substrate, area.elevation, Math.round(measured * 100) / 100, area.status, Math.round(area.progress_pct * 10) / 10, Math.round(done * 100) / 100, Math.round((measured - done) * 100) / 100, area.notes]);
    });
    const csv = rows.map((row) => row.map((value) => '"' + String(value == null ? "" : value).replace(/"/g, '""') + '"').join(",")).join("\n");
    downloadBlob(new Blob([csv], { type: "text/csv;charset=utf-8" }), "planreader_takeoff_studio.csv");
  }

  function downloadImage() {
    const image = document.getElementById("bg");
    if (!image || !image.naturalWidth) return;
    const canvas = document.createElement("canvas");
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    const context = canvas.getContext("2d");
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    context.lineWidth = Math.max(2, canvas.width / 700 * 2);
    context.font = Math.max(12, canvas.width / 90) + "px sans-serif";
    state.areas.forEach((area) => {
      if (!areaVisible(area)) return;
      const substrate = substrateFor(area.substrate);
      context.beginPath();
      area.points.forEach((item, index) => {
        const x = item.x / 100 * canvas.width;
        const y = item.y / 100 * canvas.height;
        if (index) context.lineTo(x, y); else context.moveTo(x, y);
      });
      context.closePath();
      context.fillStyle = rgba(substrate.color, 0.23);
      context.strokeStyle = substrate.color;
      context.fill();
      context.stroke();
      const center = centroid(area.points);
      const text = (area.substrate || "AREA") + " " + fmt(areaM2(area)) + " m²";
      const textWidth = context.measureText(text).width + 18;
      const textHeight = Math.max(24, canvas.width / 80);
      context.fillStyle = "rgba(15,25,38,.88)";
      context.fillRect(center.x / 100 * canvas.width - textWidth / 2, center.y / 100 * canvas.height - textHeight / 2, textWidth, textHeight);
      context.fillStyle = "#ffffff";
      context.textAlign = "center";
      context.textBaseline = "middle";
      context.fillText(text, center.x / 100 * canvas.width, center.y / 100 * canvas.height);
    });
    canvas.toBlob((blob) => { if (blob) downloadBlob(blob, "planreader_takeoff_overlay.png"); }, "image/png");
  }

  function downloadBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1200);
  }

  function resizeFrame() {
    post("streamlit:setFrameHeight", { height: Math.max(900, document.body.scrollHeight + 8) });
  }

  window.addEventListener("pointermove", handlePointerMove);
  window.addEventListener("pointerup", handlePointerUp);

  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || data.type !== "streamlit:render") return;
    const args = data.args || data;
    const revision = n(args.revision, 0);
    const image = String(args.image || "");
    if (state.image !== image || state.revision !== revision) {
      state.image = image;
      state.revision = revision;
      state.subs = Array.isArray(args.substrates) && args.substrates.length ? args.substrates.map((item) => ({
        code: String(item.code || "OTHER"),
        name: String(item.name || item.code || "Other"),
        color: String(item.color || "#80a6c9")
      })) : DEFAULT_SUBS;
      state.pxPerM = Math.max(0, n(args.px_per_m, 0));
      state.pageType = String(args.page_type || "");
      state.viewLabel = String(args.view_label || "");
      state.areas = Array.isArray(args.areas) ? args.areas.map((item, index) => normalizeArea(item, index)) : [];
      state.selected = state.areas.length ? state.areas[0].id : null;
      state.mode = "select";
      state.drag = null;
      state.draft = [];
      state.draftRect = null;
      state.history = [];
      state.future = [];
    } else {
      state.pxPerM = Math.max(0, n(args.px_per_m, state.pxPerM));
      state.pageType = String(args.page_type || state.pageType);
      state.viewLabel = String(args.view_label || state.viewLabel);
    }
    render();
  });

  window.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && state.mode === "poly" && state.draft.length >= 3) {
      event.preventDefault();
      finishPolygon();
    } else if (event.key === "Escape") {
      state.mode = "select";
      state.draft = [];
      state.draftRect = null;
      render();
    } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
      event.preventDefault();
      if (event.shiftKey) redo(); else undo();
    }
  });

  window.addEventListener("load", () => post("streamlit:componentReady", { apiVersion: 1 }));
})();
