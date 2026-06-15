// HELEN client-side glue: schedule-type toggling, clipboard, GUI checkbox sync via SSE.
(() => {
  "use strict";

  // ---- Multi-time inputs: add / remove time rows ----
  function addTimeRow(listEl) {
    const row = document.createElement("div");
    row.className = "m3-time-row";
    row.innerHTML =
      '<input type="time" name="times" required>' +
      '<button type="button" class="m3-icon-btn" data-remove-time aria-label="Uhrzeit entfernen">' +
      '<span class="material-symbols-outlined">remove</span></button>';
    listEl.appendChild(row);
    row.querySelector("input").focus();
  }

  function removeTimeRow(row) {
    const listEl = row.parentElement;
    if (listEl.querySelectorAll(".m3-time-row").length > 1) {
      row.remove();
    } else {
      const inp = row.querySelector("input");
      if (inp) inp.value = "";
    }
  }

  // Bind directly (more reliable than delegation across nested elements)
  document.querySelectorAll("[data-add-time]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      const fieldset = btn.closest("fieldset");
      const listEl = fieldset && fieldset.querySelector("[data-times-list]");
      if (listEl) addTimeRow(listEl);
    });
  });

  // Remove buttons need delegation because rows are added dynamically
  document.addEventListener("click", (e) => {
    const rmBtn = e.target.closest("[data-remove-time]");
    if (!rmBtn) return;
    e.preventDefault();
    const row = rmBtn.closest(".m3-time-row");
    if (row) removeTimeRow(row);
  });

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

  function initIndeterminateState(parentEl = document) {
    parentEl.querySelectorAll("[data-indeterminate]").forEach((el) => {
      el.indeterminate = true;
    });
  }

  function renderTasks(instances) {
    if (!instances.length) {
      return `<div class="m3-empty-hero">
        <span class="material-symbols-outlined">check_circle</span>
        <p>Keine Aufgaben für diesen Tag.</p>
      </div>`;
    }

    // Group by task_def_id preserving original order of first occurrence
    const groups = [];
    const seen = {};
    for (const i of instances) {
      const defId = i.task_def_id;
      if (!(defId in seen)) {
        const group = {
          task_def_id: defId,
          name: i.name,
          instances: []
        };
        seen[defId] = group;
        groups.push(group);
      }
      seen[defId].instances.push(i);
    }

    // Process aggregates for each group
    for (const g of groups) {
      const total = g.instances.length;
      const completedCount = g.instances.filter(i => i.completed).length;
      g.total_count = total;
      g.completed_count = completedCount;
      g.all_completed = (total > 0 && completedCount === total);
      g.some_completed = (completedCount > 0);
    }

    return `<ul class="m3-task-list">` + groups.map((g) => {
      const isIndeterminate = g.some_completed && !g.all_completed;
      return `
      <li class="m3-task-group ${g.all_completed ? "m3-task-group-done" : ""}" data-def-id="${g.task_def_id}">
        <div class="m3-task-group-header">
          <label class="m3-task-toggle">
            <input type="checkbox" data-group-toggle ${g.all_completed ? "checked" : ""} ${isIndeterminate ? "data-indeterminate=\"1\"" : ""}>
            <span class="m3-checkbox-visual">
              <span class="material-symbols-outlined">${isIndeterminate ? "remove" : "check"}</span>
            </span>
          </label>
          <div class="m3-task-group-title-wrap" data-instance-id="${g.instances[0].id}">
            <div class="m3-task-group-title">${escapeHtml(g.name)}</div>
            <div class="m3-task-group-meta" data-meta-text>
              <span class="material-symbols-outlined">medication</span>
              <span>${g.completed_count} von ${g.total_count} erledigt</span>
            </div>
          </div>
        </div>
        <div class="m3-task-group-times">
          ${g.instances.map((i) => `
          <div class="m3-task-time-item ${i.completed ? "m3-task-time-done" : ""}" data-instance-id="${i.id}">
            <label class="m3-task-time-toggle">
              <input type="checkbox" data-toggle ${i.completed ? "checked" : ""}>
              <span class="m3-checkbox-visual-small">
                <span class="material-symbols-outlined">check</span>
              </span>
              <span class="m3-task-time-label">${escapeHtml(i.due_time)}</span>
            </label>
          </div>`).join("")}
        </div>
      </li>`;
    }).join("") + `</ul>`;
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
      initIndeterminateState(list);

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
    timerWrap: modal.querySelector("[data-preview-timer-wrap]"),
    timerCircle: modal.querySelector("[data-timer-circle]"),
    timerText: modal.querySelector("[data-timer-text]"),
    timerAction: modal.querySelector("[data-timer-action]"),
  };

  let timerInterval = null;
  let timerTimeLeft = 0;
  let timerDuration = 0;

  function clearActiveTimer() {
    if (timerInterval) {
      clearInterval(timerInterval);
      timerInterval = null;
    }
    if (els && els.timerWrap) {
      els.timerWrap.classList.remove("m3-timer-finished");
      els.timerWrap.hidden = true;
    }
  }

  function setupAndStartTimer(duration) {
    if (!els || !els.timerWrap) return;
    
    timerDuration = duration;
    timerTimeLeft = duration;
    
    els.timerWrap.classList.remove("m3-timer-finished");
    els.timerWrap.hidden = false;
    els.timerText.textContent = timerTimeLeft;
    els.timerCircle.style.strokeDashoffset = 0;
    
    const circleLength = 283;
    let isPaused = false;
    els.timerAction.textContent = "Pause";
    
    const updateTimer = () => {
      if (isPaused) return;
      timerTimeLeft--;
      if (timerTimeLeft <= 0) {
        timerTimeLeft = 0;
        clearInterval(timerInterval);
        timerInterval = null;
        els.timerText.textContent = "Fertig";
        els.timerCircle.style.strokeDashoffset = circleLength;
        els.timerWrap.classList.add("m3-timer-finished");
        els.timerAction.textContent = "Neustart";
        if (navigator.vibrate) {
          navigator.vibrate([200, 100, 200]);
        }
        return;
      }
      els.timerText.textContent = timerTimeLeft;
      const offset = circleLength - (timerTimeLeft / timerDuration) * circleLength;
      els.timerCircle.style.strokeDashoffset = offset;
    };
    
    const newAction = els.timerAction.cloneNode(true);
    els.timerAction.parentNode.replaceChild(newAction, els.timerAction);
    els.timerAction = newAction;
    els.timerAction.addEventListener("click", () => {
      if (timerTimeLeft <= 0) {
        setupAndStartTimer(duration);
      } else if (isPaused) {
        isPaused = false;
        els.timerAction.textContent = "Pause";
      } else {
        isPaused = true;
        els.timerAction.textContent = "Fortsetzen";
      }
    });
    
    timerInterval = setInterval(updateTimer, 1000);
  }

  async function openPreview(instanceId) {
    if (!modal) return;
    try {
      // Clear any running timer first
      clearActiveTimer();

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

      // Start timer if duration is configured
      if (d.timer_duration && d.timer_duration > 0) {
        setupAndStartTimer(d.timer_duration);
      }
    } catch (e) {
      console.warn("preview failed", e);
    }
  }

  if (modal) {
    els.close.addEventListener("click", () => {
      clearActiveTimer();
      modal.close();
    });
    modal.addEventListener("click", (e) => {
      // click on backdrop (the dialog element itself) closes
      if (e.target === modal) {
        clearActiveTimer();
        modal.close();
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && modal.open) {
        clearActiveTimer();
        modal.close();
      }
    });
  }

  function handlePreviewTrigger(e) {
    if (e.target.closest(".m3-task-toggle") || e.target.closest(".m3-task-time-toggle")) return;
    const targetWithId = e.target.closest("[data-instance-id]");
    if (!targetWithId) return;
    const id = targetWithId.dataset.instanceId;
    if (id) {
      e.preventDefault();
      openPreview(id);
    }
  }

  list.addEventListener("click", handlePreviewTrigger);
  list.addEventListener("contextmenu", handlePreviewTrigger);

  function updateGroupStatus(groupEl) {
    const subCheckboxes = Array.from(groupEl.querySelectorAll("[data-toggle]"));
    const total = subCheckboxes.length;
    const completedCount = subCheckboxes.filter(cb => cb.checked).length;
    
    const metaText = groupEl.querySelector("[data-meta-text] span");
    if (metaText) {
      metaText.textContent = `${completedCount} von ${total} erledigt`;
    }
    
    const groupToggle = groupEl.querySelector("[data-group-toggle]");
    const groupToggleIcon = groupEl.querySelector("[data-group-toggle] + .m3-checkbox-visual .material-symbols-outlined");
    
    if (groupToggle) {
      if (completedCount === total) {
        groupToggle.checked = true;
        groupToggle.indeterminate = false;
        if (groupToggleIcon) groupToggleIcon.textContent = "check";
        groupEl.classList.add("m3-task-group-done");
      } else if (completedCount > 0) {
        groupToggle.checked = false;
        groupToggle.indeterminate = true;
        if (groupToggleIcon) groupToggleIcon.textContent = "remove";
        groupEl.classList.remove("m3-task-group-done");
      } else {
        groupToggle.checked = false;
        groupToggle.indeterminate = false;
        if (groupToggleIcon) groupToggleIcon.textContent = "check";
        groupEl.classList.remove("m3-task-group-done");
      }
    }
  }

  // Checkbox toggle (individual or group)
  list.addEventListener("change", async (ev) => {
    const cb = ev.target;
    if (!(cb instanceof HTMLInputElement)) return;

    if (cb.matches("[data-toggle]")) {
      const timeItem = cb.closest(".m3-task-time-item");
      const id = timeItem?.dataset?.instanceId;
      if (!id) return;
      
      const completed = cb.checked;
      const groupEl = cb.closest(".m3-task-group");
      const originalChecked = !completed;
      
      timeItem.classList.toggle("m3-task-time-done", completed);
      if (groupEl) updateGroupStatus(groupEl);
      
      try {
        const res = await fetch(`/api/instances/${id}/toggle`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ completed }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
      } catch (e) {
        cb.checked = originalChecked;
        timeItem.classList.toggle("m3-task-time-done", originalChecked);
        if (groupEl) updateGroupStatus(groupEl);
        console.error("toggle failed", e);
      }
    } else if (cb.matches("[data-group-toggle]")) {
      const groupEl = cb.closest(".m3-task-group");
      if (!groupEl) return;
      
      const subItems = Array.from(groupEl.querySelectorAll(".m3-task-time-item"));
      const ids = subItems.map(el => parseInt(el.dataset.instanceId)).filter(Boolean);
      const completed = cb.checked;
      
      const originalStates = subItems.map(el => {
        const input = el.querySelector("[data-toggle]");
        return {
          el: el,
          input: input,
          checked: input ? input.checked : false
        };
      });
      
      subItems.forEach(el => {
        const input = el.querySelector("[data-toggle]");
        if (input) {
          input.checked = completed;
        }
        el.classList.toggle("m3-task-time-done", completed);
      });
      updateGroupStatus(groupEl);
      
      try {
        const res = await fetch(`/api/instances/toggle-multiple`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids, completed }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
      } catch (e) {
        originalStates.forEach(state => {
          if (state.input) {
            state.input.checked = state.checked;
          }
          state.el.classList.toggle("m3-task-time-done", state.checked);
        });
        updateGroupStatus(groupEl);
        console.error("group toggle failed", e);
      }
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
        const timeItem = list.querySelector(`.m3-task-time-item[data-instance-id="${msg.id}"]`);
        if (!timeItem) return;
        const cb = timeItem.querySelector("[data-toggle]");
        const completed = !!msg.completed;
        if (cb && cb.checked !== completed) {
          cb.checked = completed;
        }
        timeItem.classList.toggle("m3-task-time-done", completed);
        const groupEl = timeItem.closest(".m3-task-group");
        if (groupEl) updateGroupStatus(groupEl);
      } catch {}
    });
    es.onopen = () => setLive(true);
    es.onerror = () => {
      setLive(false);
      es.close();
      setTimeout(connect, 3000);
    };
  };

  // ---- Web Push Notification Subscription ----
  const subscribeBtn = document.getElementById("push-subscribe-btn");
  
  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding)
      .replace(/\-/g, '+')
      .replace(/_/g, '/');
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  async function checkPushSubscription() {
    if (!subscribeBtn) return;
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      console.warn("Push messaging is not supported");
      return;
    }
    
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub) {
        // Already subscribed, hide button
        subscribeBtn.style.display = "none";
      } else {
        // Show activation button
        subscribeBtn.style.display = "inline-flex";
      }
    } catch (e) {
      console.error("Error checking subscription:", e);
    }
  }

  if (subscribeBtn) {
    subscribeBtn.addEventListener("click", async () => {
      try {
        const permission = await Notification.requestPermission();
        if (permission !== "granted") {
          alert("Benachrichtigungen wurden blockiert. Bitte in den Browsereinstellungen freigeben.");
          return;
        }

        const reg = await navigator.serviceWorker.ready;
        const resKey = await fetch("/api/push/public-key");
        if (!resKey.ok) throw new Error("VAPID public key fetch failed");
        const { publicKey } = await resKey.json();

        const subscription = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(publicKey)
        });

        // Send to backend
        const resSub = await fetch("/api/push/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(subscription)
        });

        if (resSub.ok) {
          subscribeBtn.style.display = "none";
          console.log("Push notification subscription successful");
        } else {
          throw new Error("Failed to register subscription on backend");
        }
      } catch (e) {
        console.error("Subscription process failed:", e);
        alert("Fehler bei der Aktivierung: " + e.message);
      }
    });

    checkPushSubscription();
  }

  // ---- Auto-open preview from URL parameter ?preview=ID ----
  const urlParams = new URLSearchParams(window.location.search);
  const previewId = urlParams.get("preview");
  if (previewId) {
    setTimeout(() => {
      openPreview(previewId);
    }, 300);
  }

  connect();
  initIndeterminateState(document);
})();
