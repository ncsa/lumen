// Initialises the shared graylist consent modal.
// onSuccess(modelName) is called after a successful POST.
// Returns openGraylistModal(modelName, notice, earlyNotice) for callers to trigger the dialog.
function initGraylistConsent(onSuccess) {
  let pendingModelName = null;

  function openGraylistModal(modelName, notice, earlyNotice) {
    pendingModelName = modelName;
    document.getElementById("graylistModalLabel").textContent = "Access Acknowledgment: " + modelName;
    const noticeSection = document.getElementById("graylist-notice-section");
    const noticeBody = document.getElementById("graylist-notice-body");
    if (notice) {
      noticeBody.innerHTML = DOMPurify.sanitize(marked.parse(notice));
      noticeSection.hidden = false;
    } else {
      noticeSection.hidden = true;
    }
    const earlySection = document.getElementById("graylist-early-section");
    const earlyBody = document.getElementById("graylist-early-body");
    if (earlyNotice) {
      earlyBody.innerHTML = DOMPurify.sanitize(marked.parse(earlyNotice));
      earlySection.hidden = false;
    } else {
      earlySection.hidden = true;
    }
    new bootstrap.Modal(document.getElementById("graylistModal")).show();
  }

  document.getElementById("graylist-accept-btn").addEventListener("click", async function () {
    if (!pendingModelName) return;
    const resp = await fetch("/profile/consent/" + encodeURIComponent(pendingModelName), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
    });
    if (resp.ok) {
      bootstrap.Modal.getInstance(document.getElementById("graylistModal")).hide();
      onSuccess(pendingModelName);
    } else {
      const data = await resp.json().catch(() => ({}));
      appAlert("Error: " + (data.error || "Unknown"));
    }
  });

  return openGraylistModal;
}
