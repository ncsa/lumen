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
    // Closing the dialog clears keyTtlEdit; keep this save's edit so a
    // response that lands after the close still updates the row.
    const edit = keyTtlEdit;
    const stillOpen = () => keyTtlEdit === edit;
    const fail = msg => { if (stillOpen()) error.textContent = msg; else appAlert(msg); };
    saveBtn.disabled = true;
    try {
      let resp;
      try {
        resp = await fetch(edit.ttlUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
          body: JSON.stringify({ ttl: parseInt(select.value, 10) }),
        });
      } catch {
        // No response (offline, dropped connection): the save may or may not have happened.
        fail("Error: Could not reach the server, so the key's expiry may not have changed. Reload the page to check.");
        return;
      }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        fail("Error: " + (data.error || "Failed to change the key's expiry."));
        return;
      }
      edit.onSaved(data);
      if (stillOpen()) bootstrap.Modal.getInstance(modalEl).hide();
      else if (!keyTtlEdit) { const target = edit.returnFocus(); if (target) target.focus(); }  // onSaved re-rendered the focused button
    } finally {
      saveBtn.disabled = false;
    }
  });
});
