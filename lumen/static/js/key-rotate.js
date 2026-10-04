// Rotates an API key in place: confirm, generate a new secret, POST it to
// rotateUrl, then show it once in the shared _rotated_key_modal.html dialog.
// The page reloads when the dialog closes so the table shows the new key hint.
async function rotateApiKey(rotateUrl, keyName) {
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
  modalEl.addEventListener("shown.bs.modal", () => document.getElementById("rotated-key-copy-btn").focus(), { once: true });
  modalEl.addEventListener("hidden.bs.modal", () => location.reload(), { once: true });
  bootstrap.Modal.getOrCreateInstance(modalEl).show();
}

document.addEventListener("DOMContentLoaded", () => {
  const copyBtn = document.getElementById("rotated-key-copy-btn");
  if (!copyBtn) return;
  copyBtn.addEventListener("click", () => {
    navigator.clipboard.writeText(document.getElementById("rotated-key-display").value).then(() => {
      copyBtn.textContent = "Copied!";
      setTimeout(() => { copyBtn.textContent = "Copy"; }, 2000);
    });
  });
});
