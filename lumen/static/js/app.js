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
});
