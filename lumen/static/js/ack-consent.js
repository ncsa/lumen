// Initialises the shared ack consent modal.
// onSuccess(modelName) is called after a successful POST.
// consentUrlFor(modelName) optionally overrides the consent endpoint (e.g. the
// project page posts to /projects/<id>/consent/<name>); defaults to the
// current user's /profile/consent/<name>.
// Returns openAckModal(modelName, notice, earlyNotice) for callers to trigger the dialog.
function initAckConsent(onSuccess, consentUrlFor) {
  consentUrlFor = consentUrlFor || (name => "/profile/consent/" + encodeURIComponent(name));
  let pendingModelName = null;

  function openAckModal(modelName, notice, earlyNotice) {
    pendingModelName = modelName;
    document.getElementById("ackModalLabel").textContent = "Access Acknowledgment: " + modelName;
    const noticeSection = document.getElementById("ack-notice-section");
    const noticeBody = document.getElementById("ack-notice-body");
    if (notice) {
      noticeBody.innerHTML = DOMPurify.sanitize(marked.parse(notice));
      noticeSection.hidden = false;
    } else {
      noticeSection.hidden = true;
    }
    const earlySection = document.getElementById("ack-early-section");
    const earlyBody = document.getElementById("ack-early-body");
    if (earlyNotice) {
      earlyBody.innerHTML = DOMPurify.sanitize(marked.parse(earlyNotice));
      earlySection.hidden = false;
    } else {
      earlySection.hidden = true;
    }
    new bootstrap.Modal(document.getElementById("ackModal")).show();
  }

  document.getElementById("ack-accept-btn").addEventListener("click", async function () {
    if (!pendingModelName) return;
    const resp = await fetch(consentUrlFor(pendingModelName), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
    });
    if (resp.ok) {
      bootstrap.Modal.getInstance(document.getElementById("ackModal")).hide();
      onSuccess(pendingModelName);
    } else {
      const data = await resp.json().catch(() => ({}));
      appAlert("Error: " + (data.error || "Unknown"));
    }
  });

  return openAckModal;
}

// Toggles the "Access enabled" popover shown for an already-acknowledged model
// (same content as the chat page's consented icon). Reads data-notice,
// data-early-notice and data-consented-at from btn. Auto-dismisses after 20 s
// (WCAG 2.2.1), paused while the button or popover is hovered or focused; Esc,
// an outside click or a second click closes it.
const toggleConsentPopover = (() => {
  let popover = null;
  let timer = null;

  function dismiss() {
    if (popover) { popover.dispose(); popover = null; }
    clearTimeout(timer);
  }
  const startTimer = () => { clearTimeout(timer); timer = setTimeout(dismiss, 20000); };
  const stopTimer = () => clearTimeout(timer);

  document.addEventListener("keydown", e => { if (e.key === "Escape") dismiss(); });
  document.addEventListener("click", e => {
    if (popover && !popover._element.contains(e.target) && !(popover.tip && popover.tip.contains(e.target))) dismiss();
  });

  return function (btn, placement) {
    const wasOpenHere = popover && popover._element === btn;
    dismiss();
    if (wasOpenHere) return;
    const notice = btn.dataset.notice || "";
    const earlyNotice = btn.dataset.earlyNotice || "";
    const consentedAt = btn.dataset.consentedAt || "";
    let content = notice ? DOMPurify.sanitize(marked.parse(notice)) : "";
    if (earlyNotice) content += DOMPurify.sanitize(marked.parse(earlyNotice));
    if (consentedAt) {
      const dt = new Date(consentedAt);
      content += `<p class="mb-0${notice ? " mt-1" : ""}"><strong>Access enabled.</strong> You acknowledged this model on ${dt.toLocaleString()}.</p>`;
    }
    popover = new bootstrap.Popover(btn, {
      html: true,
      content: content || "<strong>Access enabled.</strong> This model has been acknowledged.",
      trigger: "manual",
      placement: placement || "bottom",
      title: '<span class="text-warning"><i class="bi bi-exclamation-triangle-fill" aria-hidden="true"></i></span> Access Acknowledgment Required',
      customClass: "popover-warning",
    });
    const current = popover;
    btn.addEventListener("shown.bs.popover", () => {
      if (current !== popover || !current.tip) return;
      current.tip.addEventListener("mouseenter", stopTimer);
      current.tip.addEventListener("mouseleave", startTimer);
    }, { once: true });
    btn.onmouseenter = btn.onfocus = stopTimer;
    btn.onmouseleave = btn.onblur = () => { if (popover === current && !btn.matches(":hover, :focus")) startTimer(); };
    popover.show();
    if (!btn.matches(":hover, :focus")) startTimer();
  };
})();
