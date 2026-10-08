#!/usr/bin/env python
"""Audit every page of a running dev instance at phone, tablet and desktop sizes.

Visits each route as the dev_user admin (admin mode on) and as the non-admin demo
user from screenshots.py (the landing page signed out), at 375x667, 768x1024, 1024x768 and 1280x800. For every
page/size it reports:
  - horizontal overflow (document scrollWidth > clientWidth) and the elements
    that cause it (outermost elements past the viewport edge whose removal
    shrinks the page);
  - on chat, whether the input bar is fully visible without scrolling the page.

Writes report.md (pass/fail table plus details) and full-page screenshots to
OUTPUT_DIR (default responsive-audit/, git-ignored). Exits 1 if anything fails.

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


def audit(page, user, results):
    dynamic = resolve_paths(page, results["model"]) if user != "anon" else {}
    for name, path, users in ROUTES:
        if user not in users or (ONLY and name not in ONLY):
            continue
        path = path or dynamic[name]
        for w, h in SIZES:
            key = (name, user, w, h)
            if not path:
                results["rows"][key] = {"skip": "no data to link to"}
                continue
            page.set_viewport_size({"width": w, "height": h})
            resp = page.goto(BASE + path, wait_until="networkidle")
            page.wait_for_timeout(600)  # JS-rendered tables and charts
            probe = page.evaluate(PROBE_JS)
            probe["status"] = resp.status if resp else None
            shot = f"{name}-{user}-{w}x{h}.png"
            page.screenshot(path=os.path.join(OUT, shot), full_page=True)
            probe["shot"] = shot
            results["rows"][key] = probe
            print(f"{name:16} {user:5} {w}x{h}  overflow={probe['overflow']}px"
                  + (f" chat-input-visible={probe['chatInput']}" if probe["chatInput"] is not None else ""))


def cell(r):
    if "skip" in r:
        return "n/a"
    bad = []
    if r["overflow"] > 0:
        bad.append(f"overflow {r['overflow']}px")
    if r["chatInput"] is False:
        bad.append("input hidden")
    return "FAIL: " + ", ".join(bad) if bad else "pass"


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
        if cell(r) in ("pass", "n/a"):
            continue
        lines.append(f"- **{name}** ({user}, {w}×{h}, HTTP {r['status']}): {cell(r)} — `{r['shot']}`")
        for o in r["offenders"][:8]:
            lines.append(f"  - `{o}`")
    path = os.path.join(OUT, "report.md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nWrote {path}")
    return any(cell(r).startswith("FAIL") for r in rows.values())


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
