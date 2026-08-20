// Reusable name typeahead for "add entity" dialogs where the selection may be a
// user or a project. Unlike user-search.js this is keyed on the entity id, since
// projects have no email address.
// opts: { inputId, listId, hiddenId, searchUrl, onChoose, onEnter }
//   searchUrl: URL with the query appended (caller includes the trailing '?q=' or '&q=').
//   hiddenId: input that receives the chosen entity id (cleared when the text changes).
//   onChoose(id, entity): optional, called when a suggestion is picked.
//   onEnter(value): optional, called when Enter is pressed in the input.
// Returns { focus, hide, clear, selectedId } or null if the elements are missing.
function initEntitySearch(opts) {
  const input = document.getElementById(opts.inputId);
  const list = document.getElementById(opts.listId);
  const hidden = document.getElementById(opts.hiddenId);
  if (!input || !list || !hidden) return null;
  let debounce;
  let byId = new Map();
  const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  function hide() { list.classList.remove('show'); input.setAttribute('aria-expanded', 'false'); }
  function choose(id) {
    const ent = byId.get(String(id));
    if (!ent) return;
    input.value = ent.name;
    hidden.value = ent.id;
    hide();
    input.focus();
    if (opts.onChoose) opts.onChoose(ent.id, ent);
  }
  function show(entities) {
    byId = new Map(entities.map(e => [String(e.id), e]));
    if (!entities.length) { hide(); return; }
    list.innerHTML = entities.map((e, i) =>
      `<li role="option" id="${opts.listId}-opt-${i}" class="dropdown-item py-1" style="cursor:pointer" tabindex="-1" data-entity-id="${esc(e.id)}">
         <span class="fw-semibold">${esc(e.name)}</span>
         <span class="badge bg-secondary ms-1">${esc(e.type)}</span>
         ${e.email ? `<span class="text-muted small ms-1">${esc(e.email)}</span>` : ''}
       </li>`).join('');
    list.classList.add('show');
    input.setAttribute('aria-expanded', 'true');
    list.querySelectorAll('li').forEach(li =>
      li.addEventListener('mousedown', ev => { ev.preventDefault(); choose(li.dataset.entityId); }));
  }

  input.addEventListener('input', function () {
    clearTimeout(debounce);
    hidden.value = '';
    const q = this.value.trim();
    if (q.length < 2) { hide(); return; }
    debounce = setTimeout(async () => {
      try { const r = await fetch(opts.searchUrl + encodeURIComponent(q)); if (r.ok) show((await r.json()).entities); } catch (_) {}
    }, 250);
  });
  // Don't hide when focus is moving INTO the list (ArrowDown): hiding sets
  // display:none on the focused <li>, killing keyboard selection entirely.
  input.addEventListener('blur', e => {
    if (e.relatedTarget && list.contains(e.relatedTarget)) return;
    setTimeout(hide, 150);
  });
  list.addEventListener('focusout', e => {
    if (e.relatedTarget && (list.contains(e.relatedTarget) || e.relatedTarget === input)) return;
    setTimeout(hide, 150);
  });
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
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(f.dataset.entityId); }
    else if (e.key === 'Escape') { hide(); input.focus(); }
    else if (e.key === 'Home') { e.preventDefault(); list.querySelector('li')?.focus(); }
    else if (e.key === 'End') { e.preventDefault(); list.querySelector('li:last-child')?.focus(); }
  });

  return {
    focus: () => input.focus(),
    hide,
    clear: () => { input.value = ''; hidden.value = ''; hide(); },
    selectedId: () => hidden.value,
  };
}
