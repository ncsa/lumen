// Copy-ID avatars (templates/_copy_id_avatar.html): clicking the avatar copies
// the public uuid in data-copy-id. A Bootstrap popover shows the hint on hover
// and keyboard focus, and confirms the copy; the sibling .copy-id-status live
// region announces the result to screen readers. Escape hides the popover.
(function () {
  const HINT = "Click to copy ID";

  async function copyText(text) {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return;
    }
    // Fallback for plain-http deployments, where the Clipboard API is missing.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    if (!ok) throw new Error("copy failed");
  }

  document.querySelectorAll("[data-copy-id]").forEach((btn) => {
    const popover = new bootstrap.Popover(btn, {
      content: HINT,
      trigger: "hover focus",
      placement: "bottom",
    });
    const status = btn.nextElementSibling;
    const kind = btn.dataset.copyKind || "";

    btn.addEventListener("click", async () => {
      let ok = true;
      try {
        await copyText(btn.dataset.copyId);
      } catch (e) {
        ok = false;
      }
      const msg = ok ? `${kind ? kind[0].toUpperCase() + kind.slice(1) + " " : ""}ID copied to clipboard` : "Copy failed";
      popover.setContent({ ".popover-body": msg });
      popover.show();
      if (status) {
        status.textContent = "";  // so a repeated copy is announced again
        setTimeout(() => { status.textContent = msg; }, 50);
      }
    });
    // Back to the hint once the confirmation has been hidden.
    btn.addEventListener("hidden.bs.popover", () => popover.setContent({ ".popover-body": HINT }));
    btn.addEventListener("keydown", (e) => {
      if (e.key === "Escape") popover.hide();
    });
  });
})();
