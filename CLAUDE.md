# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

All times shown to the user must be in their local timezone. All times stored in the DB are UTC. In templates, emit `<span class="local-datetime" data-utc="{{ dt.strftime('%Y-%m-%dT%H:%M:%SZ') }}"></span>` and let the JS in app.js convert it to local time. Never hardcode "UTC" in displayed timestamps.
Dependencies are managed with uv, code is run with uv
Imports at top of file only; except inside Flask app factory (`create_app`) where deferred imports are required to avoid circular imports.
Styles and collors match university of illinois, see https://builder3.toolkit.illinois.edu/getting_started/index.html
When adding variables to config.yaml, make sure they are hot loaded if possible or print a warning
track changes in the CHANGELOG.md, if no unrelease section exists, then add it.
KaTeX is self-hosted in `lumen/static/vendor/katex/` (JS, CSS, fonts). Math rendering is handled in `chat.html` via `renderMarkdownWithMath()` which extracts LaTeX before marked.js can mangle it.
For local testing without OAuth or a real LLM: set `app.dev_user` in config.yaml and use `uv run dummy` (dummy backend on port 9999). See the "Local Development" section in README.md.
