# Chat

The **Chat** page (`/chat`) is the main interface for interacting with AI models.

![Chat page](/help/img/chat.png)

## Page Layout

- **Left sidebar** — Lists your conversations. Click any conversation to reload its message history here.
- **Model selector** — At the top of the chat area. Choose which model to send your message to. Graylisted models show a warning (⚠) if you haven't acknowledged them.
- **Chat area** — Displays message bubbles. User messages are right-aligned; assistant replies are left-aligned.
- **Message detail** — Each assistant message has an info icon (ⓘ). Click it to see token counts, duration, and output speed.

## Conversation Management

- **New conversation** — Click the **+ New** button in the sidebar.
- **Switch conversations** — Click any conversation in the sidebar.
- **Remove conversation** — Hover over a conversation in the sidebar and click the ✕ button. This soft-deletes it by default (visible in config). Hard delete is configured via `chat.remove: delete` in `config.yaml`.
- **Conversation title** — Automatically generated from the first message you send.

## Sending Messages

1. Select or confirm your model from the dropdown at the top.
2. Type your message in the input area at the bottom.
3. Press **Enter** to send. Hold **Shift+Enter** for a newline.
4. Click **Send** or press Enter.

## Streaming and Thinking

When sending a message, responses stream in character by character via Server-Sent Events (SSE). For models that support reasoning (configured with `supports_reasoning: true`), the response includes a collapsible "Thinking…" section showing the model's chain-of-thought before the final answer.

## File Attachments

You can attach files to a message in two ways:

- **File picker** — Click the paperclip (📎) button.
- **Drag and drop** — Drag a file onto the input bar.

Supported file types:

| Type | Extensions |
|------|-----------|
| Documents | `.txt`, `.md`, `.csv`, `.json`, `.py`, `.js`, `.ts`, `.html`, `.css`, `.xml`, `.yaml`, `.yml` |
| PDFs | `.pdf` |
| Images | `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp` |

- **Images** are base64-encoded and sent as part of a multimodal message (useful for vision-capable models).
- **Documents** are read, parsed, and prepended to your message text.
- **PDFs** are parsed with `pypdf` and appended as text.

An attachment chip appears above the input while composing a message. Click ✕ on the chip to remove it.

## Markdown and Math

Assistant responses are rendered with:

- **Markdown** — Headers, code blocks, lists, links, tables, etc.
- **Math** — Inline `$…$` and display `$$…$$` LaTeX expressions rendered with KaTeX.
- **Code blocks** — Inside fenced code blocks, `$` signs are preserved and not interpreted as math.

## Sidebar Toggle

On desktop, click the ←/→ arrow button in the header to collapse or expand the conversation sidebar. On mobile, the sidebar appears as an offcanvas panel toggled by the hamburger menu.
