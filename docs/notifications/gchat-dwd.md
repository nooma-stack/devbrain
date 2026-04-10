# Google Chat DMs via Domain-Wide Delegation

Deliver factory notifications as Google Chat direct messages, using the same domain-wide delegation pattern as the Gmail channel. DevBrain will auto-create a DM space with the target user and post messages there.

## Prerequisites

- A Google Workspace account (Google Chat DWD is **not** available on free/consumer Google accounts).
- Admin access to the Workspace to authorize DWD scopes.
- A sender user in the Workspace that the service account will impersonate (e.g. `devbrain@yourdomain.com`). This user must have Google Chat enabled.

## Setup Steps

1. **Create or reuse a GCP project**. If you already set up [Gmail DWD](gmail-dwd.md), you can reuse the same project and service account.
2. **Enable the Google Chat API**: APIs & Services → Library → "Google Chat API" → Enable.
3. **Create a service account** (or reuse the Gmail one). Enable domain-wide delegation on it.
4. **Download the JSON key** (or reuse the existing one — a single service account can hold multiple scopes).
5. **Authorize Chat scopes in Workspace Admin**: <https://admin.google.com/> → Security → Access and data control → API controls → Domain-wide delegation → edit the service account entry (or add new) and include:
   ```
   https://www.googleapis.com/auth/chat.messages.create
   https://www.googleapis.com/auth/chat.spaces.create
   ```
   If you're reusing the Gmail DWD entry, append these to the existing scope list (comma-separated in the admin UI).
6. **Place the credentials JSON** at:
   ```
   ~/.devbrain/credentials/gmail-sa.json
   ```
   (Reuse is fine — the same file works for both Gmail and Chat if both scopes are authorized.)

## Configuration

In `config/devbrain.yaml`:

```yaml
notifications:
  gchat_dwd:
    enabled: true
    credentials_path: ~/.devbrain/credentials/gmail-sa.json
    sender_email: devbrain@yourdomain.com
```

The `sender_email` is the Workspace user the bot impersonates — messages will appear in Chat as coming from that user.

## Registration

```bash
devbrain register --channel gchat_dwd:you@example.com
```

DevBrain will look up a DM space between `sender_email` and the recipient, creating one if it doesn't exist yet, and post messages there. The recipient must be in the same Workspace (or a federated one your Chat policies allow).

## Troubleshooting

**`PERMISSION_DENIED` creating a space**
: The `chat.spaces.create` scope is missing from your DWD authorization. Re-check Admin → Domain-wide delegation.

**`unauthorized_client`**
: The service account's Client ID isn't authorized in Workspace Admin, or the scope list doesn't match exactly. Scopes must be entered exactly as shown above.

**Messages never appear**
: Confirm the `sender_email` user has Google Chat turned on in the Workspace and that your org's Chat settings allow them to DM the recipient.

## When to Use This

Use Google Chat DWD when your team already lives in Google Chat and you want notifications alongside normal work conversations — no extra app needed. If your team uses Slack, Discord, or similar instead, see [webhooks.md](webhooks.md).
