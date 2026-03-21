// app.js — global utilities for Lumen
// Page-specific logic lives inline in each template's {% block scripts %}.

document.addEventListener("DOMContentLoaded", function () {
  // Auto-dismiss alerts after 5s
  document.querySelectorAll(".alert.alert-dismissible").forEach(function (el) {
    setTimeout(function () {
      const bsAlert = bootstrap.Alert.getOrCreateInstance(el);
      if (bsAlert) bsAlert.close();
    }, 5000);
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
