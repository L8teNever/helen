// HELEN client-side glue: schedule-type toggling, clipboard, GUI checkbox sync via SSE.
(() => {
  "use strict";

  // ---- Schedule form: show/hide weekday pills based on radio ----
  document.querySelectorAll("[data-schedule-form]").forEach((form) => {
    const wd = form.querySelector("[data-weekdays]");
    const radios = form.querySelectorAll("[data-schedule-radio]");
    const refresh = () => {
      const sel = form.querySelector("[data-schedule-radio]:checked");
      if (!sel || !wd) return;
      wd.hidden = sel.value !== "weekdays";
    };
    radios.forEach((r) => r.addEventListener("change", refresh));
    refresh();
  });

  // ---- Dirty-tracking: show save button only when something changed ----
  document.querySelectorAll("[data-dirty-form]").forEach((form) => {
    const actions = form.querySelector("[data-dirty-actions]");
    if (!actions) return;
    const inputs = form.querySelectorAll("input, select, textarea");
    const refresh = () => {
      let dirty = false;
      for (const el of inputs) {
        if (el.type === "checkbox" || el.type === "radio") {
          if (el.checked !== el.defaultChecked) { dirty = true; break; }
        } else if (el.tagName === "SELECT") {
          for (const opt of el.options) {
            if (opt.selected !== opt.defaultSelected) { dirty = true; break; }
          }
          if (dirty) break;
        } else {
          if (el.value !== el.defaultValue) { dirty = true; break; }
        }
      }
      actions.hidden = !dirty;
    };
    inputs.forEach((el) => {
      el.addEventListener("change", refresh);
      el.addEventListener("input", refresh);
    });
    refresh();
  });

  // ---- Copy-to-clipboard buttons ----
  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const v = btn.getAttribute("data-copy");
      try {
        await navigator.clipboard.writeText(v);
        const original = btn.innerHTML;
        btn.innerHTML = '<span class="material-symbols-outlined">check</span> Kopiert';
        setTimeout(() => { btn.innerHTML = original; }, 1500);
      } catch (e) {
        prompt("Manuell kopieren:", v);
      }
    });
  });

  // ---- GUI: only run on pages that have the task list ----
  const list = document.querySelector("[data-task-list]");
  if (!list) return;

  // ---- Day navigation: AJAX swap with slide animation, History API ----
  const dayNav = document.querySelector("[data-day-nav]");
  const topbarTitle = document.querySelector("[data-topbar-title]");
  const pillLabel = document.querySelector("[data-pill-label] > span");
  const prevLink = document.querySelector("[data-prev-link]");
  const nextLink = document.querySelector("[data-next-link]");

  const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const fmtTopbar = (d, today) => {
    if (d.toDateString() === today.toDateString()) return "Heute";
    return new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" })
      .format(d).replace(/,/g, ",");
  };
  const fmtPill = (d) => new Intl.DateTimeFormat("de-DE",
    { weekday: "long", day: "2-digit", month: "long", year: "numeric" }).format(d);

  const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
  const parseIso = (s) => { const [y, m, da] = s.split("-").map(Number); return new Date(y, m - 1, da); };

  function renderTasks(instances) {
    if (!instances.length) {
      return `<div class="m3-empty-hero">
        <span class="material-symbols-outlined">check_circle</span>
        <p>Keine Aufgaben für diesen Tag.</p>
      </div>`;
    }
    return `<ul class="m3-task-list">` + instances.map((i) => `
      <li class="m3-task ${i.completed ? "m3-task-done" : ""}" data-instance-id="${i.id}">
        <label class="m3-task-toggle">
          <input type="checkbox" data-toggle ${i.completed ? "checked" : ""}>
          <span class="m3-checkbox-visual"><span class="material-symbols-outlined">check</span></span>
        </label>
        <div class="m3-task-body">
          <div class="m3-task-title">${escapeHtml(i.name)}</div>
          <div class="m3-task-meta">
            <span class="material-symbols-outlined">schedule</span>
            ${escapeHtml(i.due_time)}${i.completed_at ? " · erledigt" : ""}
          </div>
        </div>
      </li>`).join("") + `</ul>`;
  }

  let navBusy = false;

  async function loadDay(dateStr, direction, { push = true } = {}) {
    if (navBusy || !dayNav) return;
    navBusy = true;
    const todayStr = dayNav.dataset.today;
    const outClass = direction === "next" ? "m3-slide-out-left"
                   : direction === "prev" ? "m3-slide-out-right" : null;
    const inClass  = direction === "next" ? "m3-slide-in-right"
                   : direction === "prev" ? "m3-slide-in-left"  : null;

    try {
      if (outClass) {
        list.classList.add(outClass);
        await new Promise((r) => setTimeout(r, 160));
      }
      const res = await fetch(`/api/instances?d=${encodeURIComponent(dateStr)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const tasks = await res.json();

      list.classList.remove("m3-slide-out-left", "m3-slide-out-right");
      list.innerHTML = renderTasks(tasks);
      if (inClass) {
        list.classList.add(inClass);
        setTimeout(() => list.classList.remove(inClass), 240);
      }

      const target = parseIso(dateStr);
      const today = parseIso(todayStr);
      if (topbarTitle) topbarTitle.textContent = fmtTopbar(target, today);
      if (pillLabel) pillLabel.textContent = fmtPill(target);
      const newPrev = isoDay(addDays(target, -1));
      const newNext = isoDay(addDays(target, +1));
      if (prevLink) prevLink.setAttribute("href", `/?d=${newPrev}`);
      if (nextLink) nextLink.setAttribute("href", `/?d=${newNext}`);
      dayNav.dataset.current = dateStr;

      const pillEl = pillLabel && pillLabel.parentElement;
      if (pillEl) { pillEl.classList.add("m3-flash"); setTimeout(() => pillEl.classList.remove("m3-flash"), 360); }

      if (push) {
        const url = dateStr === todayStr ? "/" : `/?d=${dateStr}`;
        history.pushState({ day: dateStr }, "", url);
      }
    } catch (e) {
      console.error("loadDay failed", e);
      list.classList.remove("m3-slide-out-left", "m3-slide-out-right");
    } finally {
      navBusy = false;
    }
  }

  const navHandler = (direction) => (e) => {
    e.preventDefault();
    const cur = parseIso(dayNav.dataset.current);
    const next = addDays(cur, direction === "next" ? 1 : -1);
    loadDay(isoDay(next), direction);
  };
  if (prevLink && dayNav) prevLink.addEventListener("click", navHandler("prev"));
  if (nextLink && dayNav) nextLink.addEventListener("click", navHandler("next"));
  if (pillLabel) pillLabel.parentElement.addEventListener("click", (e) => {
    e.preventDefault();
    if (dayNav.dataset.current === dayNav.dataset.today) return;
    loadDay(dayNav.dataset.today, "prev");
  });

  window.addEventListener("popstate", () => {
    const params = new URLSearchParams(window.location.search);
    const target = params.get("d") || dayNav?.dataset?.today;
    if (target && target !== dayNav.dataset.current) {
      const cur = parseIso(dayNav.dataset.current);
      const dir = parseIso(target) > cur ? "next" : "prev";
      loadDay(target, dir, { push: false });
    }
  });

  // ---- Preview modal (long-press on mobile, right-click on desktop) ----
  const modal = document.querySelector("[data-preview-modal]");
  const els = modal && {
    name: modal.querySelector("[data-preview-name]"),
    time: modal.querySelector("[data-preview-time]"),
    status: modal.querySelector("[data-preview-status]"),
    figure: modal.querySelector("[data-preview-figure]"),
    image: modal.querySelector("[data-preview-image]"),
    notesWrap: modal.querySelector("[data-preview-notes-wrap]"),
    notes: modal.querySelector("[data-preview-notes]"),
    close: modal.querySelector("[data-preview-close]"),
  };

  async function openPreview(instanceId) {
    if (!modal) return;
    try {
      const res = await fetch(`/api/instances/${instanceId}/preview`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      els.name.textContent = d.name || "—";
      els.time.textContent = d.due_time || "—";
      els.status.textContent = d.completed ? "Erledigt" : "Offen";
      els.status.classList.toggle("m3-chip-ok", !!d.completed);
      if (d.image_url) {
        els.image.src = d.image_url;
        els.figure.hidden = false;
      } else {
        els.image.removeAttribute("src");
        els.figure.hidden = true;
      }
      if (d.notes) {
        els.notes.textContent = d.notes;
        els.notesWrap.hidden = false;
      } else {
        els.notes.textContent = "";
        els.notesWrap.hidden = true;
      }
      if (typeof modal.showModal === "function") modal.showModal();
      else modal.setAttribute("open", "");
    } catch (e) {
      console.warn("preview failed", e);
    }
  }

  if (modal) {
    els.close.addEventListener("click", () => modal.close());
    modal.addEventListener("click", (e) => {
      // click on backdrop (the dialog element itself) closes
      if (e.target === modal) modal.close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && modal.open) modal.close();
    });
  }

  // Tap/click on the task body opens the preview modal.
  // The checkbox area (.m3-task-toggle) keeps its native behaviour and only toggles.
  list.addEventListener("click", (e) => {
    if (e.target.closest(".m3-task-toggle")) return;
    const task = e.target.closest(".m3-task");
    if (!task) return;
    const id = task.dataset.instanceId;
    if (id) openPreview(id);
  });

  // Right-click on desktop also opens the modal (no native menu).
  list.addEventListener("contextmenu", (e) => {
    if (e.target.closest(".m3-task-toggle")) return;
    const task = e.target.closest(".m3-task");
    if (!task) return;
    e.preventDefault();
    const id = task.dataset.instanceId;
    if (id) openPreview(id);
  });

  // Optimistic checkbox toggle
  list.addEventListener("change", async (ev) => {
    const cb = ev.target;
    if (!(cb instanceof HTMLInputElement) || !cb.matches("[data-toggle]")) return;
    const item = cb.closest(".m3-task");
    const id = item?.dataset?.instanceId;
    if (!id) return;
    const completed = cb.checked;
    item.classList.toggle("m3-task-done", completed);
    try {
      const res = await fetch(`/api/instances/${id}/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ completed }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      // Revert on failure
      cb.checked = !completed;
      item.classList.toggle("m3-task-done", !completed);
      console.error("toggle failed", e);
    }
  });

  // SSE subscribe for live updates from Google or other tabs
  const indicator = document.querySelector("[data-sync-indicator]");
  const setLive = (ok) => {
    if (!indicator) return;
    indicator.classList.toggle("m3-chip-ok", ok);
    indicator.classList.toggle("m3-chip-warn", !ok);
  };

  let es;
  const connect = () => {
    es = new EventSource("/events");
    es.addEventListener("instance_changed", (e) => {
      setLive(true);
      try {
        const msg = JSON.parse(e.data);
        const li = list.querySelector(`.m3-task[data-instance-id="${msg.id}"]`);
        if (!li) return;
        const cb = li.querySelector("[data-toggle]");
        if (cb && cb.checked !== !!msg.completed) {
          cb.checked = !!msg.completed;
        }
        li.classList.toggle("m3-task-done", !!msg.completed);
      } catch {}
    });
    es.onopen = () => setLive(true);
    es.onerror = () => {
      setLive(false);
      es.close();
      setTimeout(connect, 3000);
    };
  };
  connect();
})();
