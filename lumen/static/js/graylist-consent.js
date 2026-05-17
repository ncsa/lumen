// Initialises the shared graylist consent modal.
// onSuccess(modelName) is called after a successful POST.
// Returns openGraylistModal(modelName, notice) for callers to trigger the dialog.
function initGraylistConsent(onSuccess) {
  let pendingModelName = null;

  function openGraylistModal(modelName, notice) {
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
      alert("Error: " + (data.error || "Unknown"));
    }
  });

  return openGraylistModal;
}
