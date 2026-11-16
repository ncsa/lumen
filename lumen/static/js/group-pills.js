// Group picker for the Edit User dialog: the user's groups as pills with a
// remove (✕) button, plus a combobox input that suggests matching groups.
// Changes are staged locally; the caller reads changes() on Save and sends
// only the groups that were added or removed since set().
// opts: { boxId, pillsId, inputId, listId, liveId, allGroups }
//   allGroups: [{id, name, active, auto_join}] — every group, for suggestions;
//   id is the group's public uuid (a string).
// Pills are {id, name, auto_join, is_owner}; auto-join and owned groups are
// locked (no remove button), matching the group membership rules on the server.
// Returns { set(groups), changes() } or null if the elements are missing.
function initGroupPills(opts) {
  const box = document.getElementById(opts.boxId);
  const pillsEl = document.getElementById(opts.pillsId);
  const input = document.getElementById(opts.inputId);
  const list = document.getElementById(opts.listId);
  const live = document.getElementById(opts.liveId);
  if (!box || !pillsEl || !input || !list || !live) return null;
  const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  let pills = [];
  let initialIds = new Set();
  let matches = [];
  let active = -1;

  const lockReason = p => p.auto_join ? 'managed by auto-join rules' : (p.is_owner ? 'user is the owner' : '');

  function renderPills() {
    pillsEl.innerHTML = pills.map(p => {
      const reason = lockReason(p);
      const control = reason
        ? `<i class="bi bi-lock-fill" aria-hidden="true" title="${esc(reason)}"></i><span class="visually-hidden">(${esc(reason)})</span>`
        : `<button type="button" class="btn-remove-group" data-group-id="${p.id}" aria-label="Remove group ${esc(p.name)}">✕</button>`;
      return `<li class="badge bg-secondary fw-normal">${esc(p.name)} ${control}</li>`;
    }).join('');
  }

  function hide() {
    list.classList.remove('show');
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    active = -1;
  }

  function setActive(i) {
    const items = list.querySelectorAll('li');
    items.forEach((li, j) => {
      li.classList.toggle('active', j === i);
      li.setAttribute('aria-selected', j === i ? 'true' : 'false');
    });
    active = i;
    if (i >= 0) {
      input.setAttribute('aria-activedescendant', items[i].id);
      items[i].scrollIntoView({ block: 'nearest' });
    } else {
      input.removeAttribute('aria-activedescendant');
    }
  }

  function suggest() {
    const q = input.value.trim().toLowerCase();
    const taken = new Set(pills.map(p => p.id));
    matches = q
      ? opts.allGroups.filter(g => !g.auto_join && !taken.has(g.id) && g.name.toLowerCase().includes(q))
      : [];
    if (!matches.length) { hide(); return; }
    list.innerHTML = matches.map((g, i) =>
      `<li role="option" id="${opts.listId}-opt-${i}" class="dropdown-item py-1" style="cursor:pointer" aria-selected="false" data-index="${i}">
         ${esc(g.name)}${g.active ? '' : ' <span class="badge bg-light text-dark border ms-1">inactive</span>'}
       </li>`).join('');
    list.classList.add('show');
    input.setAttribute('aria-expanded', 'true');
    setActive(0);
  }

  function add(i) {
    const g = matches[i];
    if (!g) return;
    pills.push({ id: g.id, name: g.name, auto_join: false, is_owner: false });
    pills.sort((a, b) => a.name.localeCompare(b.name));
    renderPills();
    input.value = '';
    hide();
    live.textContent = `Added ${g.name}`;
    input.focus();
  }

  function remove(id) {
    const p = pills.find(x => x.id === id);
    if (!p || lockReason(p)) return;
    pills = pills.filter(x => x.id !== id);
    renderPills();
    live.textContent = `Removed ${p.name}`;
    input.focus();
  }

  pillsEl.addEventListener('click', e => {
    const btn = e.target.closest('.btn-remove-group');
    if (btn) remove(btn.dataset.groupId);
  });
  box.addEventListener('click', e => { if (e.target === box) input.focus(); });
  list.addEventListener('mousedown', e => {
    const li = e.target.closest('li');
    if (li) { e.preventDefault(); add(Number(li.dataset.index)); }
  });
  input.addEventListener('input', suggest);
  input.addEventListener('blur', () => setTimeout(hide, 150));
  input.addEventListener('keydown', e => {
    const open = list.classList.contains('show');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!open) suggest(); else setActive(Math.min(active + 1, matches.length - 1));
    } else if (e.key === 'ArrowUp' && open) {
      e.preventDefault(); setActive(Math.max(active - 1, 0));
    } else if (e.key === 'Enter') {
      // Never submit/close the dialog from the picker.
      e.preventDefault();
      if (open && active >= 0) add(active);
    } else if (e.key === 'Escape' && open) {
      // Close only the suggestions, not the modal.
      e.preventDefault(); e.stopPropagation(); hide();
    } else if (e.key === 'Backspace' && !input.value) {
      const last = [...pills].reverse().find(p => !lockReason(p));
      if (last) remove(last.id);
    }
  });

  return {
    set(groups) {
      pills = groups.map(g => ({ id: g.id, name: g.name, auto_join: g.auto_join, is_owner: g.is_owner }));
      initialIds = new Set(pills.map(p => p.id));
      renderPills();
      input.value = '';
      live.textContent = '';
      hide();
    },
    changes() {
      const ids = new Set(pills.map(p => p.id));
      return {
        add_group_ids: [...ids].filter(id => !initialIds.has(id)),
        remove_group_ids: [...initialIds].filter(id => !ids.has(id)),
      };
    },
  };
}
