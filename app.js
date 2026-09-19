  const DOT_GRADIENTS = [
    "linear-gradient(135deg, #5aa9ff, #0a67cc)",
    "linear-gradient(135deg, #58e08a, #1ea656)",
    "linear-gradient(135deg, #ffbf4d, #d17f00)",
    "linear-gradient(135deg, #c084fc, #8b2fd9)",
    "linear-gradient(135deg, #f472b6, #d1266f)",
    "linear-gradient(135deg, #5eead4, #0d9488)",
  ];
  const ITEM_H = 70, GAP = 8, SLOT = ITEM_H + GAP;
  const DROP_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    + 'stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 19h16"/></svg>';
  const DROP_WARN_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    + 'stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4"/><path d="M12 17h.01"/>'
    + '<path d="M10.3 3.9 1.9 18a1.5 1.5 0 0 0 1.3 2.2h17.6a1.5 1.5 0 0 0 1.3-2.2L13.7 3.9a1.5 1.5 0 0 0-2.6 0z"/></svg>';

  let libraries = [];      // [{label, mode, patterns, _uid}]
  let selectedUid = null;
  let uidCounter = 1;
  let dragState = null;    // {uid}
  const pillElements = new Map(); // uid -> DOM node

  let loadedFilePath = null;
  let loadedFileName = null;

  function describe(entry) {
    const verb = entry.mode === "contains" ? "contains" : "starts with";
    return verb + " \u00b7 " + entry.patterns.join(", ");
  }

  function withUids(list) {
    return list.map((e) => Object.assign({}, e, { _uid: uidCounter++ }));
  }

  function stripUids(list) {
    return list.map(({ _uid, count, ...rest }) => rest);
  }

  function buildPill(entry) {
    const node = document.createElement("div");
    node.className = "pill";
    node.dataset.uid = entry._uid;
    node.innerHTML =
      '<div class="handle">&#10247;</div>' +
      '<div class="badge"></div>' +
      '<div class="info"><div class="name"></div><div class="caption"></div></div>' +
      '<button class="edit" title="Edit">&#9998;</button>' +
      '<button class="remove" title="Remove">&#10005;</button>';
    node.querySelector(".name").textContent = entry.label;
    node.querySelector(".caption").textContent = describe(entry);
    node.querySelector(".edit").addEventListener("click", (e) => {
      e.stopPropagation();
      openDialog(entry._uid);
    });
    node.querySelector(".remove").addEventListener("click", (e) => {
      e.stopPropagation();
      removeLibrary(entry._uid);
    });
    node.addEventListener("pointerdown", (e) => onPointerDown(e, entry._uid));
    pillElements.set(entry._uid, node);
    return node;
  }

  function fullRender() {
    const container = document.getElementById("list-container");
    container.innerHTML = "";
    pillElements.clear();

    if (libraries.length === 0) {
      const FOLDER_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
        + 'stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>';
      const SEARCH_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
        + 'stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.innerHTML = loadedFilePath
        ? '<div class="icon">' + SEARCH_ICON + '</div><div class="title">No libraries detected</div>'
          + '<div class="subtitle">Try "Re-detect from File", or add one manually.</div>'
        : '<div class="icon">' + FOLDER_ICON + '</div><div class="title">No file loaded</div>'
          + '<div class="subtitle">Click "Load .vmr File...", or drop a file anywhere in this window.</div>';
      container.appendChild(empty);
      container.style.height = "100%";
      updateAllPositions();
      return;
    }

    libraries.forEach((entry, i) => {
      const node = buildPill(entry);
      node.style.transition = "none";
      node.style.transform = "translateY(" + (i * SLOT) + "px)";
      container.appendChild(node);
    });
    void container.offsetHeight; // force reflow before re-enabling transitions
    pillElements.forEach((n) => { n.style.transition = ""; });
    container.style.height = Math.max(0, libraries.length * SLOT - GAP) + "px";
    updateAllPositions();
  }

  function updateAllPositions() {
    libraries.forEach((entry, i) => {
      const node = pillElements.get(entry._uid);
      if (!node) return;
      const isDragged = dragState && dragState.uid === entry._uid;
      if (!isDragged) node.style.transform = "translateY(" + (i * SLOT) + "px)";
      const badge = node.querySelector(".badge");
      badge.textContent = i + 1;
      badge.style.background = DOT_GRADIENTS[i % DOT_GRADIENTS.length];
      const isSelected = entry._uid === selectedUid && !isDragged;
      node.classList.toggle("selected", isSelected);
    });
    document.getElementById("lib-count").textContent = loadedFilePath ? String(libraries.length) : "-";
    setDot("lib-dot", libraries.length > 0 ? "ok" : "idle");
    updateToolbar();
  }

  function updateToolbar() {
    const idx = selectedUid === null ? null : libraries.findIndex((l) => l._uid === selectedUid);
    document.getElementById("btn-up").disabled = (idx === null || idx === 0);
    document.getElementById("btn-down").disabled = (idx === null || idx === libraries.length - 1);
  }

  function moveSelected(delta) {
    if (selectedUid === null) return;
    const idx = libraries.findIndex((l) => l._uid === selectedUid);
    const newIdx = idx + delta;
    if (newIdx < 0 || newIdx >= libraries.length) return;
    const tmp = libraries[idx];
    libraries[idx] = libraries[newIdx];
    libraries[newIdx] = tmp;
    updateAllPositions();
    persist();
  }

  function removeLibrary(uid) {
    libraries = libraries.filter((l) => l._uid !== uid);
    if (selectedUid === uid) selectedUid = null;
    fullRender();
    persist();
  }

  function onPointerDown(e, uid) {
    if (e.target.closest(".remove") || e.target.closest(".edit")) return;
    const node = pillElements.get(uid);
    const startIdx = libraries.findIndex((l) => l._uid === uid);
    const dragStartY = e.clientY;
    const dragBaseY = startIdx * SLOT;
    let moved = false;
    dragState = { uid };
    selectedUid = uid;
    node.classList.add("dragging");
    node.setPointerCapture(e.pointerId);
    updateAllPositions();

    function onMove(ev) {
      const dy = ev.clientY - dragStartY;
      if (Math.abs(dy) > 3) moved = true;
      node.style.transform = "translateY(" + (dragBaseY + dy) + "px)";
      const targetIdx = Math.max(0, Math.min(libraries.length - 1, Math.round((dragBaseY + dy) / SLOT)));
      const curIdx = libraries.findIndex((l) => l._uid === uid);
      if (targetIdx !== curIdx) {
        const item = libraries.splice(curIdx, 1)[0];
        libraries.splice(targetIdx, 0, item);
        updateAllPositions();
      }
    }

    function onUp() {
      node.removeEventListener("pointermove", onMove);
      node.removeEventListener("pointerup", onUp);
      node.classList.remove("dragging");
      dragState = null;
      const finalIdx = libraries.findIndex((l) => l._uid === uid);
      node.style.transform = "translateY(" + (finalIdx * SLOT) + "px)";
      selectedUid = uid;
      updateAllPositions();
      persist();
    }

    node.addEventListener("pointermove", onMove);
    node.addEventListener("pointerup", onUp);
  }

  function persist() {
    if (window.pywebview && loadedFilePath) {
      window.pywebview.api.save_libraries(loadedFilePath, stripUids(libraries));
    }
  }

  // Shared dot vocabulary for the whole status card:
  //   idle = grey (not ready), busy = blue (working), ok = green (good), error = red
  function dotColor(kind) {
    return kind === "ok" ? "var(--accent-green)"
      : kind === "error" ? "var(--accent-red)"
      : kind === "busy" ? "var(--accent-blue)"
      : "var(--text-faint)";
  }

  function setDot(id, kind) {
    document.getElementById(id).style.background = dotColor(kind);
  }

  const READY_TEXT = "ready - set priority order, then Create VMR";

  function showStatus(kind, text) {
    setDot("status-dot", kind);
    document.getElementById("status-text").textContent = text;
  }

  function switchTab(name) {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === name + "-view"));
    document.getElementById("toolbar-collapse").classList.toggle("collapsed", name === "log");
    moveTabIndicator(name);
  }

  function moveTabIndicator(name) {
    const btn = document.querySelector('.tab-btn[data-tab="' + name + '"]');
    const indicator = document.getElementById("tab-indicator");
    if (!btn || !indicator) return;
    indicator.style.width = btn.offsetWidth + "px";
    indicator.style.setProperty("--indicator-x", btn.offsetLeft + "px");
  }

  let editingUid = null;

  function openDialog(uid) {
    editingUid = uid || null;
    const entry = editingUid ? libraries.find((l) => l._uid === editingUid) : null;
    document.getElementById("dialog-title").textContent = entry ? "Edit Library" : "Add Library";
    document.getElementById("add-submit").textContent = entry ? "Save" : "Add";
    document.getElementById("add-name").value = entry ? entry.label : "";
    document.getElementById("add-mode").value = entry && entry.mode === "contains" ? "Contains" : "Starts with";
    document.getElementById("add-patterns").value = entry ? entry.patterns.join(", ") : "";
    document.getElementById("add-error").textContent = "";
    document.getElementById("add-dialog-overlay").classList.remove("hidden");
    document.getElementById("add-name").focus();
  }

  function closeDialog() {
    document.getElementById("add-dialog-overlay").classList.add("hidden");
    editingUid = null;
  }

  function submitDialog() {
    const name = document.getElementById("add-name").value.trim();
    const mode = document.getElementById("add-mode").value === "Contains" ? "contains" : "prefix";
    const patterns = document.getElementById("add-patterns").value.split(",").map((s) => s.trim()).filter(Boolean);
    const errEl = document.getElementById("add-error");
    if (!name) { errEl.textContent = "Please enter a library name."; return; }
    if (!patterns.length) { errEl.textContent = "Please enter at least one match text."; return; }
    const duplicate = libraries.some((l) => l.label === name && l._uid !== editingUid);
    if (duplicate) { errEl.textContent = "A library with that name already exists."; return; }

    if (editingUid) {
      const entry = libraries.find((l) => l._uid === editingUid);
      entry.label = name;
      entry.mode = mode;
      entry.patterns = patterns;
      selectedUid = editingUid;
      fullRender();
    } else {
      const entry = { label: name, mode, patterns, _uid: uidCounter++ };
      libraries.push(entry);
      selectedUid = entry._uid;
      fullRender();
    }
    persist();
    closeDialog();
  }

  function setFileStatus(kind, name) {
    setDot("file-dot", kind);
    document.getElementById("file-name").textContent = name;
  }

  function setCreateButtonEnabled(enabled) {
    const btn = document.getElementById("btn-choose");
    btn.disabled = !enabled;
    btn.textContent = enabled ? "Create VMR" : "Load a .vmr file first";
  }

  function setFileDependentControlsEnabled(enabled) {
    document.getElementById("btn-add").disabled = !enabled;
    document.getElementById("btn-reset").disabled = !enabled;
    setCreateButtonEnabled(enabled);
  }

  function showLoading(text) {
    document.getElementById("loading-text").textContent = text;
    document.getElementById("loading-overlay").classList.remove("hidden");
    document.getElementById("btn-load").disabled = true;
    document.getElementById("btn-reset").disabled = true;
    document.getElementById("btn-choose").disabled = true;
  }

  function hideLoading() {
    document.getElementById("loading-overlay").classList.add("hidden");
    document.getElementById("btn-load").disabled = false;
    if (loadedFilePath) {
      document.getElementById("btn-reset").disabled = false;
      document.getElementById("btn-choose").disabled = false;
    }
  }

  function appendDetectedLog(name, detected, headline) {
    const logEl = document.getElementById("log-output");
    logEl.textContent += headline + "\n";
    logEl.textContent += "Detected " + detected.length + " librar" + (detected.length === 1 ? "y" : "ies") + ":\n";
    detected.forEach(function(d) {
      const countText = ("count" in d) ? " - " + d.count + " models" : "";
      logEl.textContent += "  " + d.label + countText + " (starts with \"" + d.patterns[0] + "\")\n";
    });
    logEl.textContent += "-".repeat(50) + "\n";
    logEl.scrollTop = logEl.scrollHeight;
  }

  async function loadFile() {
    if (!window.pywebview) return;
    showStatus("busy", "waiting for file selection...");
    const picked = await window.pywebview.api.pick_file();
    if (picked.cancelled) {
      showStatus("idle", loadedFilePath ? READY_TEXT : "waiting for a file");
      return;
    }
    await loadFromPath(picked.path, picked.name);
  }

  async function loadFromPath(path, name) {
    setFileStatus("busy", name);
    showLoading("Scanning " + name + "...");
    const result = await window.pywebview.api.detect_or_load(path);
    const logEl = document.getElementById("log-output");
    if (result.error) {
      logEl.textContent += "ERROR: " + result.error + "\n";
      showStatus("error", "error - see log");
      setFileStatus("error", name);
      switchTab("log");
      hideLoading();
      return;
    }

    loadedFilePath = path;
    loadedFileName = name;
    setFileStatus("ok", name);
    setFileDependentControlsEnabled(true);

    libraries = withUids(result.libraries);
    selectedUid = null;
    fullRender();
    persist();

    const headline = result.source === "saved"
      ? "Loaded: " + name + " (using your saved priority order)"
      : "Loaded: " + name + " (no saved order yet, detected from file)";
    appendDetectedLog(name, result.libraries, headline);
    hideLoading();
    showStatus("idle", READY_TEXT);
  }

  function setDropOverlay(mode, subtitle) {
    const overlay = document.getElementById("drop-overlay");
    overlay.classList.toggle("invalid", mode === "invalid");
    document.getElementById("drop-icon").innerHTML = mode === "invalid" ? DROP_WARN_ICON : DROP_ICON;
    document.getElementById("drop-title").textContent = mode === "invalid" ? "Not a .vmr file" : "Drop to load";
    document.getElementById("drop-subtitle").textContent = subtitle;
    overlay.classList.remove("hidden");
    document.getElementById("app").classList.add("dragging");
  }

  function hideDropOverlay() {
    document.getElementById("drop-overlay").classList.add("hidden");
    document.getElementById("app").classList.remove("dragging");
  }

  // A dialog (Add/Edit Library, What's new) covers the same window a file could be
  // dropped onto - dragging or dropping while one is open would show the overlay
  // behind/around the dialog and could still swap out the loaded file underneath it.
  function isModalOpen() {
    return !document.getElementById("add-dialog-overlay").classList.contains("hidden")
      || !document.getElementById("whatsnew-overlay").classList.contains("hidden");
  }

  // Called from Python (run_gui's document.on("drop", ...) handler) once it has
  // resolved the real filesystem path - the plain browser drop event below only
  // ever sees a filename, never a usable path.
  function handleDroppedFile(path) {
    hideDropOverlay();
    if (isModalOpen()) return;
    loadFromPath(path, path.split(/[\\/]/).pop());
  }

  function wireDragAndDrop() {
    let dragDepth = 0;
    let revertTimer = null;

    document.addEventListener("dragenter", (e) => {
      if (!e.dataTransfer || !e.dataTransfer.types.includes("Files") || isModalOpen()) return;
      e.preventDefault();
      dragDepth++;
      clearTimeout(revertTimer);
      setDropOverlay("hover", loadedFilePath ? "Replaces the current session." : "Release to load this .vmr.");
    });
    document.addEventListener("dragover", (e) => {
      if (e.dataTransfer && e.dataTransfer.types.includes("Files") && !isModalOpen()) e.preventDefault();
    });
    document.addEventListener("dragleave", () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) hideDropOverlay();
    });
    document.addEventListener("drop", (e) => {
      if (!e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
      dragDepth = 0;
      if (isModalOpen()) { e.preventDefault(); return; }
      e.preventDefault();
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (file && !/\.vmr$/i.test(file.name)) {
        setDropOverlay("invalid", '"' + file.name + '" - drop a .vmr file instead.');
        revertTimer = setTimeout(hideDropOverlay, 1800);
        return;
      }
      // A valid-looking drop falls through to Python's own drop listener, which
      // resolves the real path and calls handleDroppedFile() above.
    });
  }

  async function redetect() {
    if (!window.pywebview || !loadedFilePath) return;
    setFileStatus("busy", loadedFileName);
    showLoading("Re-scanning " + loadedFileName + "...");
    const result = await window.pywebview.api.redetect(loadedFilePath);
    const logEl = document.getElementById("log-output");
    if (result.error) {
      logEl.textContent += "ERROR: " + result.error + "\n";
      showStatus("error", "error - see log");
      setFileStatus("error", loadedFileName);
      switchTab("log");
      hideLoading();
      return;
    }
    libraries = withUids(result.libraries);
    selectedUid = null;
    fullRender();
    persist();
    appendDetectedLog(loadedFileName, result.libraries, "Re-detected libraries from: " + loadedFileName);
    setFileStatus("ok", loadedFileName);
    hideLoading();
    showStatus("idle", READY_TEXT);
  }

  async function createVmr() {
    if (!window.pywebview || !loadedFilePath) return;
    showLoading("Processing " + loadedFileName + "...");
    showStatus("busy", "processing " + loadedFileName + "...");
    const clean = stripUids(libraries);
    const result = await window.pywebview.api.create_vmr(loadedFilePath, clean);
    const logEl = document.getElementById("log-output");
    if (result.error) {
      logEl.textContent += "ERROR: " + result.error + "\n";
      showStatus("error", "error - see log");
      switchTab("log");
    } else {
      const banner = "=".repeat(50);
      logEl.textContent += banner + "\n";
      logEl.textContent += "Priority order: " + result.order.join(" > ") + " > (everything else)\n";
      logEl.textContent += "Processing: " + result.input_name + "\n";
      logEl.textContent += "Total rules processed: " + result.total + "\n";
      logEl.textContent += "Excluded tokens containing: " + result.excluded.join(", ") + "\n";
      logEl.textContent += "Rules dropped (empty after exclusion): " + result.dropped + "\n";
      logEl.textContent += "Output folder: " + result.output_dir + "\n";
      result.files.forEach(function(f) {
        logEl.textContent += "  " + f.label + " - " + f.count + " rules -> " + f.path + "\n";
      });
      logEl.textContent += "\nNext: in vPilot, go to Settings > Model Matching > Custom Rules,\n";
      logEl.textContent += "click 'Add Custom Rule Set(s)', select all the files above, then\n";
      logEl.textContent += "use Move Up/Move Down so they match the order shown here.\n";
      logEl.textContent += "\nVMR CREATED - " + result.files.length + " file"
        + (result.files.length === 1 ? "" : "s") + " written\n";
      logEl.textContent += banner + "\n\n";
      logEl.scrollTop = logEl.scrollHeight;
      showStatus("ok", "done - " + result.files.length + " files written");
      switchTab("log");
    }
    hideLoading();
  }

  let updateInfo = null;
  let updateChangelogOpen = false;
  let updatePolling = null;

  function showUpdateBanner(info) {
    updateInfo = info;
    document.getElementById("update-version").textContent = "v" + info.version;
    document.getElementById("update-changelog-text").textContent = info.changelog || "No changelog provided.";
    const errEl = document.getElementById("update-error");
    errEl.textContent = "";
    errEl.classList.remove("status");
    document.getElementById("update-progress").classList.add("hidden");
    document.getElementById("update-collapse").classList.add("open");
  }

  function toggleUpdateChangelog() {
    updateChangelogOpen = !updateChangelogOpen;
    document.getElementById("update-changelog-collapse").classList.toggle("open", updateChangelogOpen);
    document.getElementById("update-toggle-log").textContent = updateChangelogOpen ? "Show less" : "What's new";
  }

  function dismissUpdateBanner() {
    document.getElementById("update-collapse").classList.remove("open");
  }

  // Per-digit odometer for the "NN%" in the downloading status line -
  // swapping the number via textContent every poll tick reads as flicker, so
  // instead each digit that actually changed slides out while its new digit
  // slides in (50 -> 60 only rolls the tens digit; the "%" is a static text
  // node outside the roller and is never touched by an update).
  let pctRoll = null; // {wrapper, cols: [{col, track, current}], value} once
                       // built; torn down whenever setUpdateStatusText/error
                       // path replaces #update-error's content

  function setUpdateStatusText(text) {
    const el = document.getElementById("update-error");
    el.textContent = text;
    el.classList.add("status");
    pctRoll = null; // el.textContent just tore down any roller markup
  }

  function buildDigitCol(digit, lineHeightPx) {
    const col = document.createElement("span");
    col.className = "pct-digit-col";
    col.style.height = lineHeightPx + "px";
    col.style.lineHeight = lineHeightPx + "px";
    const track = document.createElement("span");
    track.className = "pct-digit-track";
    const current = document.createElement("span");
    current.className = "pct-digit-val";
    current.style.height = lineHeightPx + "px";
    current.style.lineHeight = lineHeightPx + "px";
    current.textContent = digit;
    track.appendChild(current);
    col.appendChild(track);
    return { col, track, current };
  }

  function ensurePctRoll() {
    const el = document.getElementById("update-error");
    if (pctRoll && pctRoll.wrapper.parentElement === el) return pctRoll;
    el.textContent = "";
    el.classList.add("status");
    el.appendChild(document.createTextNode("downloading update... "));
    // Match the roller's row height to how this same text actually renders,
    // rather than guessing a CSS line-height multiplier - a guess doesn't
    // reliably match Segoe UI's real metrics and made the whole banner
    // render taller than it should.
    const lineHeightPx = el.getBoundingClientRect().height;
    const wrapper = document.createElement("span");
    wrapper.className = "pct-roll";
    el.appendChild(wrapper);
    el.appendChild(document.createTextNode("%")); // static - never rebuilt by a digit update
    pctRoll = { wrapper, cols: [], value: null, lineHeightPx };
    return pctRoll;
  }

  // Settles a column to a single child at rest, discarding any digit left
  // mid-flight by a poll tick arriving before the previous one's
  // transitionend fired.
  function settleDigitCol(colObj) {
    Array.from(colObj.track.children).forEach((child) => { if (child !== colObj.current) child.remove(); });
    colObj.track.style.transition = "none";
    colObj.track.style.transform = "translateY(0)";
    void colObj.track.offsetHeight;
    colObj.track.style.transition = "";
  }

  function animateDigitCol(colObj, newDigit, goingUp, lineHeightPx) {
    settleDigitCol(colObj);
    const outgoing = colObj.current;
    const incoming = document.createElement("span");
    incoming.className = "pct-digit-val";
    incoming.style.height = lineHeightPx + "px";
    incoming.style.lineHeight = lineHeightPx + "px";
    incoming.textContent = newDigit;

    if (goingUp) {
      colObj.track.appendChild(incoming);
      requestAnimationFrame(() => { colObj.track.style.transform = "translateY(-50%)"; });
    } else {
      colObj.track.insertBefore(incoming, outgoing);
      colObj.track.style.transition = "none";
      colObj.track.style.transform = "translateY(-50%)";
      void colObj.track.offsetHeight;
      colObj.track.style.transition = "";
      requestAnimationFrame(() => { colObj.track.style.transform = "translateY(0)"; });
    }
    colObj.current = incoming;

    colObj.track.addEventListener("transitionend", function onEnd() {
      colObj.track.removeEventListener("transitionend", onEnd);
      outgoing.remove();
      colObj.track.style.transition = "none";
      colObj.track.style.transform = "translateY(0)";
      void colObj.track.offsetHeight;
      colObj.track.style.transition = "";
    });
  }

  function setDownloadPercent(pct) {
    const roll = ensurePctRoll();
    if (pct === roll.value) return;

    const newDigits = String(pct).split("");

    // Digit count changed (crossing 9<->10, 99<->100, or the first build) -
    // rare enough that a plain rebuild (no per-digit roll) is fine rather
    // than animating a column sliding in/out of existence.
    if (roll.cols.length !== newDigits.length) {
      roll.wrapper.innerHTML = "";
      roll.cols = newDigits.map((d) => {
        const built = buildDigitCol(d, roll.lineHeightPx);
        roll.wrapper.appendChild(built.col);
        return built;
      });
      roll.value = pct;
      return;
    }

    // roll.cols.length matching newDigits.length means String(roll.value) is
    // necessarily the same length too, so no alignment/padding is needed here.
    const oldDigits = String(roll.value).split("");
    const goingUp = pct > roll.value;
    for (let i = 0; i < newDigits.length; i++) {
      if (oldDigits[i] === newDigits[i]) continue; // this digit didn't change - leave it alone
      animateDigitCol(roll.cols[i], newDigits[i], goingUp, roll.lineHeightPx);
    }
    roll.value = pct;
  }

  function pollUpdateProgress() {
    if (updatePolling) clearInterval(updatePolling);
    updatePolling = setInterval(async () => {
      const state = await window.pywebview.api.get_update_progress();
      if (state.phase === "downloading") {
        const pct = state.total ? Math.min(100, Math.round((state.downloaded / state.total) * 100)) : 0;
        document.getElementById("update-progress-fill").style.width = pct + "%";
        setDownloadPercent(pct);
      } else if (state.phase === "verifying") {
        document.getElementById("update-progress-fill").style.width = "100%";
        setUpdateStatusText("verifying download...");
      } else if (state.phase === "restarting") {
        document.getElementById("update-progress-fill").style.width = "100%";
        setUpdateStatusText("restarting...");
        // App is about to close itself and relaunch as the new version - nothing else to do.
      } else if (state.phase === "error") {
        clearInterval(updatePolling);
        updatePolling = null;
        const el = document.getElementById("update-error");
        el.classList.remove("status");
        el.textContent = state.error || "Update failed.";
        pctRoll = null; // el.textContent just tore down any roller markup
        document.getElementById("update-now-btn").disabled = false;
        document.getElementById("update-dismiss").disabled = false;
      }
    }, 400);
  }

  async function startUpdate() {
    if (!updateInfo || !window.pywebview) return;
    document.getElementById("update-now-btn").disabled = true;
    document.getElementById("update-dismiss").disabled = true;
    setDownloadPercent(0);
    document.getElementById("update-progress-fill").style.width = "0%";
    document.getElementById("update-progress").classList.remove("hidden");
    const res = await window.pywebview.api.start_update(
      updateInfo.download_url, updateInfo.version, updateInfo.changelog,
      updateInfo.asset_size, updateInfo.asset_digest
    );
    if (!res.started) return;
    pollUpdateProgress();
  }

  async function checkForUpdate() {
    if (!window.pywebview) return;
    try {
      const info = await window.pywebview.api.check_for_update();
      if (info && info.available) {
        showUpdateBanner(info);
      } else if (info && info.error) {
        document.getElementById("log-output").textContent += "Update check failed: " + info.error + "\n";
      }
    } catch (e) {
      document.getElementById("log-output").textContent += "Update check failed: " + e + "\n";
    }
  }

  async function checkPendingChangelog() {
    if (!window.pywebview) return;
    const pending = await window.pywebview.api.get_pending_changelog();
    if (pending && pending.version) {
      document.getElementById("whatsnew-version").textContent = pending.version;
      document.getElementById("whatsnew-text").textContent = pending.changelog || "No changelog provided.";
      document.getElementById("whatsnew-overlay").classList.remove("hidden");
    }
  }

  function wireStaticEvents() {
    document.getElementById("btn-up").addEventListener("click", () => moveSelected(-1));
    document.getElementById("btn-down").addEventListener("click", () => moveSelected(1));
    document.getElementById("btn-add").addEventListener("click", () => openDialog(null));
    document.getElementById("btn-load").addEventListener("click", loadFile);
    document.getElementById("btn-reset").addEventListener("click", redetect);
    document.getElementById("btn-choose").addEventListener("click", createVmr);
    document.getElementById("add-cancel").addEventListener("click", closeDialog);
    document.getElementById("add-submit").addEventListener("click", submitDialog);
    document.getElementById("add-dialog-overlay").addEventListener("click", (e) => {
      if (e.target.id === "add-dialog-overlay") closeDialog();
    });
    document.getElementById("update-toggle-log").addEventListener("click", toggleUpdateChangelog);
    document.getElementById("update-now-btn").addEventListener("click", startUpdate);
    document.getElementById("update-dismiss").addEventListener("click", dismissUpdateBanner);
    document.getElementById("whatsnew-close").addEventListener("click", () => {
      document.getElementById("whatsnew-overlay").classList.add("hidden");
    });
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });
    wireDragAndDrop();
  }

  async function showAppVersion() {
    if (!window.pywebview) return;
    document.getElementById("app-version").textContent = "v" + await window.pywebview.api.get_app_version();
  }

  function init() {
    wireStaticEvents();
    libraries = [];
    fullRender();
    const indicator = document.getElementById("tab-indicator");
    indicator.style.transition = "none";
    moveTabIndicator("priority");
    requestAnimationFrame(() => { indicator.style.transition = ""; });
    showAppVersion();
    checkPendingChangelog();
    checkForUpdate();
  }

  // pywebview injects window.pywebview asynchronously, and it can arrive a
  // few ms after this script starts running - checking it once, synchronously,
  // to decide whether to listen for "pywebviewready" is a race: if it loses,
  // no listener is ever attached and the ready event fires to nobody. Always
  // listen for it, and use DOMContentLoaded only as a timed fallback for
  // viewing this HTML directly in a plain browser with no Python bridge.
  let inited = false;
  function safeInit() {
    if (inited) return;
    inited = true;
    init();
  }
  window.addEventListener("pywebviewready", safeInit);
  if (window.pywebview) {
    safeInit();
  } else {
    window.addEventListener("DOMContentLoaded", () => {
      let attempts = 0;
      const poll = setInterval(() => {
        attempts++;
        if (inited) { clearInterval(poll); return; }
        if (window.pywebview || attempts >= 20) {
          clearInterval(poll);
          safeInit();
        }
      }, 100);
    });
  }
