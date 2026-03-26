"""OAuth consent page rendering."""

from __future__ import annotations

import html


def render_consent_page(
    client_name: str,
    scope_descriptions: list[str],
    form_action: str,
    request_id: str,
) -> str:
    """Render a self-contained HTML consent page."""
    escaped_name = html.escape(client_name)
    scopes_html = "\n".join(f"<li>{html.escape(desc)}</li>" for desc in scope_descriptions)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Authorize — Pincer Agent</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: #f5f5f5;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      padding: 1rem;
    }}
    .card {{
      background: white;
      border-radius: 12px;
      box-shadow: 0 4px 24px rgba(0,0,0,0.1);
      padding: 2rem;
      max-width: 420px;
      width: 100%;
    }}
    h1 {{ font-size: 1.4rem; margin: 0 0 0.5rem; color: #111; }}
    .app-name {{ color: #3a7bd5; font-weight: 600; }}
    .subtitle {{ color: #555; font-size: 0.9rem; margin-bottom: 1.5rem; }}
    ul {{ padding-left: 1.2rem; margin: 0 0 1.5rem; color: #333; line-height: 1.7; }}
    .actions {{ display: flex; gap: 0.75rem; }}
    .btn {{
      flex: 1;
      padding: 0.7rem;
      border: none;
      border-radius: 8px;
      font-size: 1rem;
      font-weight: 500;
      cursor: pointer;
      transition: opacity 0.15s;
    }}
    .btn:hover {{ opacity: 0.85; }}
    .btn-approve {{ background: #22c55e; color: white; }}
    .btn-deny {{ background: #ef4444; color: white; }}
    .footer {{ margin-top: 1.5rem; font-size: 0.75rem; color: #888; text-align: center; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Authorize <span class="app-name">{escaped_name}</span>?</h1>
    <p class="subtitle">This application is requesting the following permissions:</p>
    <ul>
      {scopes_html}
    </ul>
    <form method="POST" action="{html.escape(form_action)}">
      <input type="hidden" name="request_id" value="{html.escape(request_id)}">
      <div class="actions">
        <button type="submit" name="decision" value="approve" class="btn btn-approve">Approve</button>
        <button type="submit" name="decision" value="deny" class="btn btn-deny">Deny</button>
      </div>
    </form>
    <p class="footer">Pincer Agent &mdash; Personal AI Assistant</p>
  </div>
</body>
</html>"""


def format_channel_consent(client_name: str, scope_descriptions: list[str]) -> str:
    """Plain-text consent message for Telegram/WhatsApp."""
    scopes = "\n".join(f"  • {desc}" for desc in scope_descriptions)
    return (
        f"🔐 *Authorization Request*\n\n"
        f"*{client_name}* is requesting access to your Pincer agent:\n\n"
        f"{scopes}\n\n"
        f"Reply *yes* to approve or *no* to deny."
    )
