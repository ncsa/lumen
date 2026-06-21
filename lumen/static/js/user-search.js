// Reusable name/email typeahead for "add user" style dialogs (add manager, add config user).
// opts: { inputId, listId, searchUrl, onChoose, onEnter }
//   searchUrl: URL with the query appended (caller includes the trailing '?q=' or '&q=').
//   onChoose(email): optional, called when a suggestion is picked.
//   onEnter(value): optional, called when Enter is pressed in the input.
// Returns { focus, hide, clear } or null if the elements are missing.
function initUserSearch(opts) {
  const input = document.getElementById(opts.inputId);
  const list = document.getElementById(opts.listId);
  if (!input || !list) return null;
  let debounce;
  const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  function hide() { list.classList.remove('show'); input.setAttribute('aria-expanded', 'false'); }
  function choose(email) { input.value = email; hide(); input.focus(); if (opts.onChoose) opts.onChoose(email); }
  function show(users) {
    if (!users.length) { hide(); return; }
    list.innerHTML = users.map((u, i) =>
      `<li role="option" id="${opts.listId}-opt-${i}" class="dropdown-item py-1" style="cursor:pointer" tabindex="-1" data-email="${esc(u.email)}">
         <span class="fw-semibold">${esc(u.name)}</span>
         <span class="text-muted small ms-1">${esc(u.email)}</span>
       </li>`).join('');
    list.classList.add('show');
    input.setAttribute('aria-expanded', 'true');
    list.querySelectorAll('li').forEach(li =>
      li.addEventListener('mousedown', e => { e.preventDefault(); choose(li.dataset.email); }));
  }

  input.addEventListener('input', function () {
    clearTimeout(debounce);
    const q = this.value.trim();
    if (q.length < 2) { hide(); return; }
    debounce = setTimeout(async () => {
      try { const r = await fetch(opts.searchUrl + encodeURIComponent(q)); if (r.ok) show((await r.json()).users); } catch (_) {}
    }, 250);
  });
  input.addEventListener('blur', () => setTimeout(hide, 150));
  input.addEventListener('keydown', e => {
    const first = list.querySelector('li');
    if (e.key === 'ArrowDown' && first) { e.preventDefault(); first.focus(); }
    else if (e.key === 'Enter' && opts.onEnter) { e.preventDefault(); opts.onEnter(input.value); }
    else if (e.key === 'Escape') hide();
  });
  list.addEventListener('keydown', e => {
    const f = document.activeElement;
    if (e.key === 'ArrowDown') { e.preventDefault(); f.nextElementSibling?.focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); f.previousElementSibling ? f.previousElementSibling.focus() : input.focus(); }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(f.dataset.email); }
    else if (e.key === 'Escape') { hide(); input.focus(); }
    else if (e.key === 'Home') { e.preventDefault(); list.querySelector('li')?.focus(); }
    else if (e.key === 'End') { e.preventDefault(); list.querySelector('li:last-child')?.focus(); }
  });

  return {
    focus: () => input.focus(),
    hide,
    clear: () => { input.value = ''; hide(); },
  };
}
