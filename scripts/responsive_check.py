#!/usr/bin/env python
"""Audit every page of a running dev instance at phone, tablet and desktop sizes.

Visits each route as the dev_user admin (admin mode on) and as the non-admin demo
user from screenshots.py (the landing page signed out), at 375x667, 768x1024, 1024x768 and 1280x800. For every
page/size it reports:
  - horizontal overflow (document scrollWidth > clientWidth) and the elements
    that cause it (outermost elements past the viewport edge whose removal
    shrinks the page);
  - on chat, whether the input bar is fully visible without scrolling the page;
  - on chat at tablet and desktop widths (768 and up, where the sidebar is an
    inline column), whether the #chatSidebar and .chat-main widths stay put
    (within 1px) when a long thinking block and a long answer are injected into
    #chat-messages, with the conversation list empty and with conversations, and
    whether the columns still compute to flex 0 0 260px / 1 1 0px. The DOM is
    injected directly because the dummy backend doesn't stream reasoning.

Writes report.md (pass/fail table plus details) and full-page screenshots to
OUTPUT_DIR (default responsive-audit/, git-ignored). Exits 1 if anything fails,
including a page that cannot be measured: an unexpected HTTP status or redirect
(e.g. bounced to login), a detail page with no link to follow, or a chat page
without its input bar.

Prerequisites are the same as scripts/screenshots.py (see scripts/README.md):
a running dummy backend and app with a dev config that sets `app.dev_user`, and
`uv run python -m playwright install chromium`. Then, with the SAME CONFIG_YAML:
    CONFIG_YAML=./dev.config.yaml uv run python scripts/responsive_check.py

Env vars: BASE_URL (default http://localhost:5001), OUTPUT_DIR (default
responsive-audit), MODEL (default: first active model), CHROME_PATH (fallback
browser executable), ONLY (comma-separated route names to limit the run).
"""
import os
import sys
from http import HTTPStatus
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from sqlalchemy import select

from lumen import create_app
from lumen.extensions import db
from lumen.models.entity import Entity
from lumen.models.entity_manager import EntityManager
from lumen.models.group import Group
from lumen.models.group_member import GroupMember
from screenshots import BASE, DEMO_USER, ensure_demo_data, launch, urlhost

OUT = os.environ.get("OUTPUT_DIR", "responsive-audit")
ONLY = {n for n in os.environ.get("ONLY", "").split(",") if n}
SIZES = [(375, 667), (768, 1024), (1024, 768), (1280, 800)]

# (name, path or None when resolved at run time, users who visit it)
BOTH = ("admin", "user")
ROUTES = [
    ("landing", "/", ("anon",)),  # signed-in visitors are redirected to chat
    ("chat", "/chat", BOTH),
    ("profile", "/profile", BOTH),
    ("usage", "/usage", BOTH),
    ("models", "/models", BOTH),
    ("model-detail", None, BOTH),
    ("projects", "/projects", BOTH),
    ("project-detail", None, BOTH),
    ("groups", "/groups", BOTH),
    ("group-detail", None, BOTH),
    ("connect", "/connect", BOTH),
    ("help", "/help/", BOTH),
    ("admin-users", "/admin/users", ("admin",)),
    ("admin-config", "/admin/config", ("admin",)),
    ("admin-analytics", "/admin/analytics", ("admin",)),
    ("oauth-device", "/device", BOTH),
    ("oauth-consent", None, BOTH),
    ("404", "/responsive-check-missing", BOTH),
]
# Routes that intentionally answer with something other than 200 at the same path.
EXPECTED_STATUS = {"404": HTTPStatus.NOT_FOUND}
EXPECTED_REDIRECTS = {"/admin/analytics": "/usage"}

# Returns {overflow, offenders[], chatInput} for the current page at scroll 0.
PROBE_JS = """() => {
  document.documentElement.style.scrollBehavior = 'auto';  // Bootstrap's smooth scroll would delay this
  window.scrollTo(0, 0);
  const doc = document.documentElement;
  const vw = doc.clientWidth;
  const overflow = doc.scrollWidth - vw;
  const describe = el => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const cls = [...el.classList].slice(0, 3);
    if (cls.length) s += '.' + cls.join('.');
    return s;
  };
  const sticksOut = el => {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.position === 'fixed') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && (r.right > vw + 1 || r.left < -1);
  };
  // Hiding a real cause shrinks the page; content that scrolls inside its own box does not.
  const causesOverflow = el => {
    const prev = el.style.display;
    el.style.display = 'none';
    const width = doc.scrollWidth;
    el.style.display = prev;
    return width < doc.scrollWidth;
  };
  const offenders = [];
  if (overflow > 0) {
    for (const el of document.body.querySelectorAll('*')) {
      // Report only the outermost element: its parent still fits.
      if (!sticksOut(el) || (el.parentElement && sticksOut(el.parentElement)) || !causesOverflow(el)) continue;
      const r = el.getBoundingClientRect();
      offenders.push(`${describe(el)} (${Math.round(r.width)}px wide, right edge ${Math.round(r.right)}px)`);
    }
  }
  let chatInput = null;
  const bar = document.querySelector('.chat-input-bar');
  if (bar) {
    const r = bar.getBoundingClientRect();
    chatInput = r.top >= 0 && r.left >= 0 && r.bottom <= window.innerHeight + 1 && r.right <= vw + 1;
  }
  return {overflow, offenders, chatInput};
}"""


# Layout-stability check for chat, for viewports where the sidebar is inline (CSS md breakpoint).
LAYOUT_MIN_WIDTH = 768
LAYOUT_TOLERANCE = 1  # px
# Computed flex values that keep the columns sized from the row, not their content (app.css).
EXPECTED_FLEX = {"sidebar": "0 0 260px", "main": "1 1 0px"}

# Returns [{list, flex: {sidebar, main}, before: {sidebar, main}, after: {sidebar, main}}], one entry per
# conversation-list state (emptied, then filled with three items), or null when the chat columns are
# missing. Injects the same DOM shape as chat.html's streaming code and sidebar, then restores the page.
LAYOUT_JS = """() => {
  const sidebar = document.getElementById('chatSidebar');
  const main = document.querySelector('.chat-main');
  const messages = document.getElementById('chat-messages');
  const list = document.getElementById('conv-list');
  if (!sidebar || !main || !messages || !list) return null;
  const widths = () => ({sidebar: sidebar.getBoundingClientRect().width, main: main.getBoundingClientRect().width});
  const longLine = (word, n) => Array.from({length: n}, (_, i) => word + i).join(' ');
  const measure = label => {
    const flex = {sidebar: getComputedStyle(sidebar).flex, main: getComputedStyle(main).flex};
    const before = widths();
    const row = document.createElement('div');
    row.className = 'd-flex mb-2 justify-content-start';
    const wrapper = document.createElement('div');
    wrapper.className = 'd-flex flex-column align-items-start';
    wrapper.style.maxWidth = '65%';
    const details = document.createElement('details');
    details.className = 'thinking-block mb-1';
    details.open = true;
    const summary = document.createElement('summary');
    summary.textContent = 'Thinking…';
    const pre = document.createElement('pre');
    pre.className = 'thinking-text';
    pre.textContent = Array.from({length: 40}, () => longLine('reasoning', 60)).join('\\n')
      + '\\n' + 'x'.repeat(600);
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble chat-bubble-assistant';
    bubble.textContent = longLine('answer', 400) + ' ' + 'y'.repeat(600);
    details.append(summary, pre);
    wrapper.append(details, bubble);
    row.appendChild(wrapper);
    messages.appendChild(row);
    const after = widths();
    row.remove();
    return {list: label, flex, before, after};
  };
  const convItem = i => {
    const item = document.createElement('div');
    item.className = 'conv-item d-flex align-items-center px-3 py-2';
    const info = document.createElement('div');
    info.className = 'flex-fill overflow-hidden me-1';
    const title = document.createElement('div');
    title.className = 'small fw-medium text-truncate';
    title.textContent = `Chat ${i}`;  // short, so the list doesn't pin the sidebar's min-content width
    const preview = document.createElement('div');
    preview.className = 'msg-meta text-truncate';
    preview.textContent = 'Hello';
    const remove = document.createElement('button');
    remove.className = 'conv-remove-btn btn btn-link btn-sm p-0 text-muted';
    remove.textContent = '✕';
    info.append(title, preview);
    item.append(info, remove);
    return item;
  };
  const saved = [...list.childNodes];
  list.replaceChildren();
  const runs = [measure('empty list')];
  list.replaceChildren(...Array.from({length: 3}, (_, i) => convItem(i + 1)));
  runs.push(measure('3 conversations'));
  list.replaceChildren(...saved);
  return runs;
}"""


def layout_problems(layout):
    """Why the chat columns are unstable: a width that moved or a flex value that changed."""
    if layout is None:
        return ["chat columns missing"]
    bad = []
    for run in layout:
        for col, expected in EXPECTED_FLEX.items():
            if run["flex"][col] != expected:
                bad.append(f"{col} flex {run['flex'][col]}, expected {expected} ({run['list']})")
        for col in ("sidebar", "main"):
            before, after = run["before"][col], run["after"][col]
            if abs(after - before) > LAYOUT_TOLERANCE:
                bad.append(f"{col} width {before:.0f}→{after:.0f}px ({run['list']})")
    return bad


def unknown_routes(only):
    """Names in ONLY that match no route, so a typo fails instead of auditing nothing."""
    return sorted(only - {name for name, _, _ in ROUTES})


def ensure_audit_data(app):
    """On top of screenshots.py's demo data, give both users a group to open and
    the demo user the demo project, so every detail page has a link to follow."""
    with app.app_context():
        users = db.session.execute(
            select(Entity).where(Entity.entity_type == "user",
                                 Entity.email.in_([DEMO_USER, app.config.get("DEV_USER")]))
        ).scalars().all()
        project = db.session.execute(
            select(Entity).filter_by(name="example-bot", entity_type="project")
        ).scalar_one()
        group = db.session.execute(select(Group).filter_by(name="responsive-demo")).scalar_one_or_none()
        if not group:
            group = Group(name="responsive-demo", active=True)
            db.session.add(group)
            db.session.flush()
        for user in users:
            if not db.session.execute(
                select(GroupMember).filter_by(group_id=group.id, entity_id=user.id)
            ).scalar_one_or_none():
                db.session.add(GroupMember(group_id=group.id, entity_id=user.id))
            if not db.session.execute(
                select(EntityManager).filter_by(user_entity_id=user.id, project_entity_id=project.id)
            ).scalar_one_or_none():
                db.session.add(EntityManager(user_entity_id=user.id, project_entity_id=project.id))
        db.session.commit()


def enable_admin_mode(page):
    page.goto(BASE + "/profile", wait_until="networkidle")
    result = page.evaluate("""async () => {
        const token = document.querySelector('meta[name="csrf-token"]').content;
        const resp = await fetch('/profile/settings/admin-mode', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': token},
            body: JSON.stringify({enabled: true}),
        });
        const body = await resp.json().catch(() => null);
        return {status: resp.status, body: body};
    }""")
    if result["status"] != 200 or not (result["body"] or {}).get("admin_mode"):
        raise RuntimeError(f"could not enable admin mode: HTTP {result['status']} {result['body']}")


def first_link(page, path, selector):
    page.goto(BASE + path, wait_until="networkidle")
    page.wait_for_timeout(800)  # list pages fill their tables from JSON
    el = page.query_selector(selector)
    return el.get_attribute("href") if el else None


def consent_path(page):
    """Start a device-code request and return the consent page for it."""
    resp = page.request.post(BASE + "/oauth/device_authorization", form={
        "client_id": "lumen-cli", "name": "responsive-check", "author": "lumen"})
    if not resp.ok:
        return None
    return f"/device?code={resp.json()['user_code']}"


def resolve_paths(page, model):
    return {
        "model-detail": f"/models/{model}" if model else None,
        "project-detail": first_link(page, "/projects", "a[href^='/projects/']"),
        "group-detail": first_link(page, "/groups", "a[href^='/groups/']"),
        "oauth-consent": consent_path(page),
    }


def problems(name, path, r):
    """Why a measured row fails: layout problems, or a measurement of the wrong page."""
    if not path:
        return ["no URL to visit (missing demo data?)"]
    bad = []
    expected = EXPECTED_STATUS.get(name, HTTPStatus.OK)
    if r["status"] != expected:
        bad.append(f"HTTP {r['status']}, expected {expected.value}")
    landed = urlparse(r["url"]).path
    if landed != EXPECTED_REDIRECTS.get(urlparse(path).path, urlparse(path).path):
        bad.append(f"redirected to {landed}")
    if r["overflow"] > 0:
        bad.append(f"overflow {r['overflow']}px")
    if name == "chat" and r["chatInput"] is None:
        bad.append("input missing")
    elif r["chatInput"] is False:
        bad.append("input hidden")
    if "layout" in r:
        bad += layout_problems(r["layout"])
    return bad


def audit(page, user, results):
    dynamic = resolve_paths(page, results["model"]) if user != "anon" else {}
    for name, path, users in ROUTES:
        if user not in users or (ONLY and name not in ONLY):
            continue
        path = path or dynamic[name]
        for w, h in SIZES:
            key = (name, user, w, h)
            if not path:
                results["rows"][key] = {"problems": problems(name, path, None),
                                        "status": None, "shot": "no screenshot", "offenders": []}
                continue
            page.set_viewport_size({"width": w, "height": h})
            resp = page.goto(BASE + path, wait_until="networkidle")
            page.wait_for_timeout(600)  # JS-rendered tables and charts
            probe = page.evaluate(PROBE_JS)
            if name == "chat" and w >= LAYOUT_MIN_WIDTH:
                probe["layout"] = page.evaluate(LAYOUT_JS)
            probe["status"] = resp.status if resp else None
            probe["url"] = page.url
            probe["problems"] = problems(name, path, probe)
            shot = f"{name}-{user}-{w}x{h}.png"
            page.screenshot(path=os.path.join(OUT, shot), full_page=True)
            probe["shot"] = shot
            results["rows"][key] = probe
            print(f"{name:16} {user:5} {w}x{h}  {cell(probe)}")


def cell(r):
    return "FAIL: " + ", ".join(r["problems"]) if r["problems"] else "pass"


def write_report(results):
    rows = results["rows"]
    lines = ["# Responsive audit", "",
             "| Page | User | " + " | ".join(f"{w}×{h}" for w, h in SIZES) + " |",
             "|---|---|" + "---|" * len(SIZES)]
    for name, _, users in ROUTES:
        for user in users:
            if (name, user, *SIZES[0]) not in rows:
                continue
            cells = [cell(rows[(name, user, w, h)]) for w, h in SIZES]
            lines.append(f"| {name} | {user} | " + " | ".join(cells) + " |")
    lines += ["", "## Details", ""]
    for (name, user, w, h), r in rows.items():
        if not r["problems"]:
            continue
        lines.append(f"- **{name}** ({user}, {w}×{h}, HTTP {r['status']}): {cell(r)} — `{r['shot']}`")
        for o in r["offenders"][:8]:
            lines.append(f"  - `{o}`")
    path = os.path.join(OUT, "report.md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nWrote {path}")
    return any(r["problems"] for r in rows.values())


def main():
    unknown = unknown_routes(ONLY)
    if unknown:
        sys.exit(f"Unknown ONLY route name(s): {', '.join(unknown)}. "
                 f"Valid names: {', '.join(name for name, _, _ in ROUTES)}")
    app = create_app()
    model, cookie, _ = ensure_demo_data(app)
    ensure_audit_data(app)
    os.makedirs(OUT, exist_ok=True)
    results = {"model": model, "rows": {}}

    with sync_playwright() as pw:
        b = launch(pw)

        actx = b.new_context()
        page = actx.new_page()
        page.goto(BASE + "/devlogin", wait_until="networkidle")
        enable_admin_mode(page)
        audit(page, "admin", results)
        actx.close()

        uctx = b.new_context()
        uctx.add_cookies([{"name": "session", "value": cookie, "domain": urlhost(), "path": "/"}])
        audit(uctx.new_page(), "user", results)
        uctx.close()

        nctx = b.new_context()
        audit(nctx.new_page(), "anon", results)
        nctx.close()
        b.close()

    sys.exit(1 if write_report(results) else 0)


if __name__ == "__main__":
    main()
