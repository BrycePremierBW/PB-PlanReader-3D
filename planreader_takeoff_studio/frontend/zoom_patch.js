(function () {
  "use strict";

  let zoom = 1;
  const MIN_ZOOM = 0.5;
  const MAX_ZOOM = 4.0;
  const STEP = 0.25;

  function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }

  function injectStyles() {
    if (document.getElementById("pbZoomPatchStyles")) return;
    const style = document.createElement("style");
    style.id = "pbZoomPatchStyles";
    style.textContent =
      ".pb-zoom-controls{display:flex;gap:4px;align-items:center;margin-left:8px}" +
      ".pb-zoom-controls button{border:1px solid #344154;background:#151d28;color:#e8eef5;border-radius:4px;padding:4px 8px;font-size:11px;font-weight:700;cursor:pointer}" +
      ".pb-zoom-controls button:hover{background:#1d2a39;border-color:#4c617b}" +
      ".pb-zoom-readout{min-width:43px;text-align:center;color:#c1ccda;font-size:11px;font-weight:700}" +
      ".canvas-wrap.pb-zoomed{overflow:auto!important;align-items:flex-start!important;justify-content:flex-start!important}" +
      ".canvas-wrap.pb-zoomed .stage{max-width:none!important;max-height:none!important}" +
      ".canvas-wrap.pb-zoomed .stage img{max-width:none!important;max-height:none!important}";
    document.head.appendChild(style);
  }

  function elements() {
    const wrap = document.getElementById("canvasWrap");
    const stage = wrap ? wrap.querySelector(".stage") : null;
    const image = stage ? stage.querySelector("img") : null;
    return { wrap: wrap, stage: stage, image: image };
  }

  function updateReadout() {
    const readout = document.getElementById("pbZoomReadout");
    if (readout) readout.textContent = Math.round(zoom * 100) + "%";
  }

  function applyZoom() {
    const els = elements();
    if (!els.wrap || !els.stage || !els.image) {
      updateReadout();
      return;
    }

    if (Math.abs(zoom - 1) < 0.001) {
      els.wrap.classList.remove("pb-zoomed");
      els.stage.style.width = "";
      els.stage.style.maxWidth = "";
      els.stage.style.maxHeight = "";
      els.image.style.width = "";
      els.image.style.height = "";
      els.image.style.maxWidth = "";
      els.image.style.maxHeight = "";
      els.wrap.scrollLeft = 0;
      els.wrap.scrollTop = 0;
    } else {
      els.wrap.classList.add("pb-zoomed");
      els.stage.style.width = (zoom * 100) + "%";
      els.stage.style.maxWidth = "none";
      els.stage.style.maxHeight = "none";
      els.image.style.width = "100%";
      els.image.style.height = "auto";
      els.image.style.maxWidth = "none";
      els.image.style.maxHeight = "none";
    }
    updateReadout();
  }

  function setZoom(nextZoom) {
    const els = elements();
    let xRatio = 0.5;
    let yRatio = 0.5;
    if (els.wrap && els.wrap.scrollWidth > 0 && els.wrap.scrollHeight > 0) {
      xRatio = (els.wrap.scrollLeft + els.wrap.clientWidth / 2) / els.wrap.scrollWidth;
      yRatio = (els.wrap.scrollTop + els.wrap.clientHeight / 2) / els.wrap.scrollHeight;
    }

    zoom = clamp(nextZoom, MIN_ZOOM, MAX_ZOOM);
    applyZoom();

    window.requestAnimationFrame(function () {
      const updated = elements();
      if (!updated.wrap || Math.abs(zoom - 1) < 0.001) return;
      updated.wrap.scrollLeft = Math.max(0, xRatio * updated.wrap.scrollWidth - updated.wrap.clientWidth / 2);
      updated.wrap.scrollTop = Math.max(0, yRatio * updated.wrap.scrollHeight - updated.wrap.clientHeight / 2);
    });
  }

  function ensureControls() {
    injectStyles();
    const toolbar = document.querySelector(".canvas-toolbar");
    if (!toolbar) return;

    let controls = document.getElementById("pbZoomControls");
    if (!controls) {
      controls = document.createElement("span");
      controls.id = "pbZoomControls";
      controls.className = "pb-zoom-controls";
      controls.innerHTML =
        '<button type="button" id="pbZoomOut" title="Zoom out">−</button>' +
        '<button type="button" id="pbZoomFit" title="Fit drawing to editor">Fit</button>' +
        '<button type="button" id="pbZoomIn" title="Zoom in">+</button>' +
        '<span class="pb-zoom-readout" id="pbZoomReadout">100%</span>';

      const scale = document.getElementById("scaleTxt");
      if (scale && scale.parentNode === toolbar) {
        toolbar.insertBefore(controls, scale);
      } else {
        toolbar.appendChild(controls);
      }

      document.getElementById("pbZoomOut").addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        setZoom(zoom - STEP);
      });
      document.getElementById("pbZoomFit").addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        setZoom(1);
      });
      document.getElementById("pbZoomIn").addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        setZoom(zoom + STEP);
      });
    }

    applyZoom();
  }

  const observer = new MutationObserver(function () {
    ensureControls();
  });

  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("resize", applyZoom);
  ensureControls();
})();
