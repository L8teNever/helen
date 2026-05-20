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
