// Rotates an API key in place: confirm, generate a new secret, POST it to
// rotateUrl, then show it once in the shared _rotated_key_modal.html dialog.
// The page reloads when the dialog closes so the table shows the new key hint.
// rotateInFlight blocks a second rotation (e.g. a double-click) while one is pending.
let rotateInFlight = false;
async function rotateApiKey(rotateUrl, keyName) {
  if (rotateInFlight) return;
  rotateInFlight = true;
  try {
    await rotateApiKeyOnce(rotateUrl, keyName);
  } finally {
    rotateInFlight = false;
  }
}

async function rotateApiKeyOnce(rotateUrl, keyName) {
  if (!await appConfirm(
    `Rotate API key "${keyName}"? The current key stops working immediately; usage stats are kept.`,
    { title: "Rotate API Key", okLabel: "Rotate", okClass: "btn-danger" })) return;

  const gen = await fetch("/profile/keys/generate");
  if (!gen.ok) { appAlert("Failed to generate a new key."); return; }
  const { key } = await gen.json();

  const resp = await fetch(rotateUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
    body: JSON.stringify({ key }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) { appAlert("Error: " + (data.error || "Failed to rotate key.")); return; }

  const modalEl = document.getElementById("rotatedKeyModal");
  document.getElementById("rotated-key-name").textContent = data.name;
  document.getElementById("rotated-key-display").value = data.key;
  document.getElementById("rotated-key-copy-status").textContent = "";
  modalEl.addEventListener("shown.bs.modal", () => document.getElementById("rotated-key-copy-btn").focus(), { once: true });
  modalEl.addEventListener("hidden.bs.modal", () => location.reload(), { once: true });
  bootstrap.Modal.getOrCreateInstance(modalEl).show();
}

document.addEventListener("DOMContentLoaded", () => {
  const copyBtn = document.getElementById("rotated-key-copy-btn");
  if (!copyBtn) return;
  // Copy feedback goes to a live region and stays until the dialog closes.
  const status = document.getElementById("rotated-key-copy-status");
  copyBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(document.getElementById("rotated-key-display").value).then(
      () => { status.textContent = "Key copied to clipboard."; },
      () => { status.textContent = "Copy failed. Select the key and copy it manually."; });
  });
});
