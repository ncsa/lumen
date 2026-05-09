// app.js — global utilities for Lumen
// Page-specific logic lives inline in each template's {% block scripts %}.

const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';

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
