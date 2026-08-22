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
