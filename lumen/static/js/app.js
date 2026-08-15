// app.js — global utilities for Lumen
// Page-specific logic lives inline in each template's {% block scripts %}.

let csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';

// The CSRF token baked into the page at load expires after WTF_CSRF_TIME_LIMIT
// (1h). Refresh it periodically and when the tab regains focus so long-lived
// pages (e.g. an open chat) keep working. Existing fetch calls read the global
// csrfToken at send time, so updating it here keeps them valid with no changes.
async function refreshCsrfToken() {
  try {
    const resp = await fetch("/csrf-token", { headers: { "Accept": "application/json" }, cache: "no-store" });
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.token) {
      csrfToken = data.token;
      const meta = document.querySelector('meta[name="csrf-token"]');
      if (meta) meta.content = data.token;
    }
  } catch (e) {
    /* network hiccup — keep the existing token and retry on the next tick */
  }
}

setInterval(refreshCsrfToken, 30 * 60 * 1000);
document.addEventListener("visibilitychange", function () {
  if (document.visibilityState === "visible") refreshCsrfToken();
});

document.addEventListener("DOMContentLoaded", function () {
  // Auto-dismiss alerts after 20s; pause timer on hover/focus (WCAG 2.2.1)
  document.querySelectorAll(".alert.alert-dismissible").forEach(function (el) {
    var remaining = 20000, start = Date.now(), timer;
    function startTimer() {
      start = Date.now();
      timer = setTimeout(function () {
        var bsAlert = bootstrap.Alert.getOrCreateInstance(el);
        if (bsAlert) bsAlert.close();
      }, remaining);
    }
    function pauseTimer() {
      clearTimeout(timer);
      remaining -= Date.now() - start;
    }
    el.addEventListener("mouseenter", pauseTimer);
    el.addEventListener("mouseleave", startTimer);
    el.addEventListener("focusin", pauseTimer);
    el.addEventListener("focusout", startTimer);
    startTimer();
  });

  // Announcement banner dismiss — hide if this exact message was last dismissed;
  // clear the stored key when no banner is present so re-posting the same message shows it again.
  const banner = document.querySelector(".announcement-banner[data-announcement-key]");
  if (banner) {
    const current = banner.dataset.announcementKey;
    if (localStorage.getItem("announcement-last-dismissed") === current) {
      banner.style.display = "none";
    }
    banner.querySelector(".announcement-dismiss").addEventListener("click", function () {
      localStorage.setItem("announcement-last-dismissed", current);
      banner.style.display = "none";
    });
  } else {
    localStorage.removeItem("announcement-last-dismissed");
  }

  // Convert UTC ISO timestamps to local time for display
  document.querySelectorAll(".local-datetime[data-utc]").forEach(function (el) {
    const d = new Date(el.dataset.utc);
    if (!isNaN(d)) {
      el.textContent = d.toLocaleString([], {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit",
      });
    }
  });
});

// ── Styled dialogs replacing native alert()/confirm()/prompt() ─────────────
// Each returns a promise that resolves when the dialog closes:
// appAlert → true, appConfirm → true/false, appPrompt → string or null.
(function () {
  function openDialog({ title, message, okLabel, okClass, showCancel, showInput, inputValue }) {
    const el = document.getElementById("app-dialog");
    const okBtn = document.getElementById("app-dialog-ok");
    const input = document.getElementById("app-dialog-input");

    document.getElementById("app-dialog-title").textContent = title;
    const msgEl = document.getElementById("app-dialog-message");
    msgEl.textContent = message;
    msgEl.hidden = showInput; // for prompts the message becomes the input label
    document.getElementById("app-dialog-input-label").textContent = message;
    document.getElementById("app-dialog-input-wrap").hidden = !showInput;
    input.value = inputValue || "";
    document.getElementById("app-dialog-cancel").hidden = !showCancel;
    okBtn.textContent = okLabel;
    okBtn.className = "btn " + okClass;

    const modal = bootstrap.Modal.getOrCreateInstance(el);
    return new Promise(function (resolve) {
      let result = showInput ? null : !showCancel; // dismissing an alert still resolves true
      const onOk = () => { result = showInput ? input.value : true; modal.hide(); };
      const onKey = e => { if (e.key === "Enter") { e.preventDefault(); onOk(); } };
      const onHidden = () => {
        okBtn.removeEventListener("click", onOk);
        input.removeEventListener("keydown", onKey);
        el.removeEventListener("hidden.bs.modal", onHidden);
        resolve(result);
      };
      okBtn.addEventListener("click", onOk);
      if (showInput) {
        input.addEventListener("keydown", onKey);
        el.addEventListener("shown.bs.modal", () => input.focus(), { once: true });
      }
      el.addEventListener("hidden.bs.modal", onHidden);
      modal.show();
    });
  }

  window.appAlert = (message, title = "Notice") =>
    openDialog({ title, message, okLabel: "OK", okClass: "btn-primary",
                 showCancel: false, showInput: false });
  window.appConfirm = (message, opts = {}) =>
    openDialog({ title: opts.title || "Please Confirm", message,
                 okLabel: opts.okLabel || "OK", okClass: opts.okClass || "btn-primary",
                 showCancel: true, showInput: false });
  window.appPrompt = (message, opts = {}) =>
    openDialog({ title: opts.title || "Input Needed", message,
                 okLabel: opts.okLabel || "OK", okClass: "btn-primary",
                 showCancel: true, showInput: true, inputValue: opts.value });
})();
