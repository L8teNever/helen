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

  // Right-click and long-press handlers
  list.addEventListener("contextmenu", (e) => {
    const task = e.target.closest(".m3-task");
    if (!task) return;
    e.preventDefault();
    const id = task.dataset.instanceId;
    if (id) openPreview(id);
  });

  let pressTimer = null;
  let pressedItem = null;
  const PRESS_MS = 450;
  list.addEventListener("touchstart", (e) => {
    const task = e.target.closest(".m3-task");
    if (!task) return;
    pressedItem = task;
    pressTimer = setTimeout(() => {
      pressTimer = null;
      if (pressedItem && pressedItem.dataset.instanceId) {
        // prevent the upcoming click from toggling the checkbox
        pressedItem.dataset.suppressClick = "1";
        openPreview(pressedItem.dataset.instanceId);
        if (navigator.vibrate) navigator.vibrate(15);
      }
    }, PRESS_MS);
  }, { passive: true });
  const cancelPress = () => {
    if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
    pressedItem = null;
  };
  list.addEventListener("touchend", cancelPress);
  list.addEventListener("touchmove", cancelPress);
  list.addEventListener("touchcancel", cancelPress);

  // Suppress the synthetic click that follows a long-press touch
  list.addEventListener("click", (e) => {
    const task = e.target.closest(".m3-task");
    if (task && task.dataset.suppressClick) {
      delete task.dataset.suppressClick;
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);

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
