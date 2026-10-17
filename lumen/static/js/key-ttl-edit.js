// Edits an API key's expiry in the shared _key_ttl_modal.html dialog: POST the
// chosen TTL (seconds) to ttlUrl and pass the response ({id, name, expires_at})
// to onSaved. When the dialog closes, focus goes to returnFocus(), which looks
// up the row's "Edit expiry" button again since onSaved may re-render the table.
let keyTtlEdit = null;
function editKeyTtl(ttlUrl, keyName, onSaved, returnFocus) {
  keyTtlEdit = { ttlUrl, onSaved, returnFocus };
  const select = document.getElementById("edit-key-ttl-select");
  select.value = [...select.options].find(o => o.defaultSelected).value;
  document.getElementById("key-ttl-name").textContent = keyName;
  document.getElementById("key-ttl-error").textContent = "";
  bootstrap.Modal.getOrCreateInstance(document.getElementById("keyTtlModal")).show();
}

document.addEventListener("DOMContentLoaded", () => {
  const modalEl = document.getElementById("keyTtlModal");
  if (!modalEl) return;
  const select = document.getElementById("edit-key-ttl-select");
  const saveBtn = document.getElementById("key-ttl-save-btn");
  const error = document.getElementById("key-ttl-error");
  modalEl.addEventListener("shown.bs.modal", () => select.focus());
  modalEl.addEventListener("hidden.bs.modal", () => {
    const target = keyTtlEdit && keyTtlEdit.returnFocus();
    keyTtlEdit = null;
    if (target) target.focus();
  });
  saveBtn.addEventListener("click", async () => {
    if (!keyTtlEdit) return;
    saveBtn.disabled = true;
    try {
      const resp = await fetch(keyTtlEdit.ttlUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
        body: JSON.stringify({ ttl: parseInt(select.value, 10) }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { error.textContent = "Error: " + (data.error || "Failed to change the key's expiry."); return; }
      keyTtlEdit.onSaved(data);
      bootstrap.Modal.getInstance(modalEl).hide();
    } finally {
      saveBtn.disabled = false;
    }
  });
});
