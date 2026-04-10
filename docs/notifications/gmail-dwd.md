# Gmail via Domain-Wide Delegation

Send notification emails from a Google Workspace address using a service account with domain-wide delegation (DWD). Unlike SMTP, this does not require per-user passwords — a single service account can send as any user in your Workspace.

## When to Use This vs SMTP

Use **Gmail DWD** when:

- You run Google Workspace and want notifications to come from a domain address (e.g. `devbrain@yourdomain.com`).
- You don't want to manage SMTP app passwords per user.
- You want centralized auth via a single service account credential.

Use **SMTP** ([smtp.md](smtp.md)) when:

- You're on free Gmail (DWD is not available).
- You don't have Workspace admin rights.
- You want a simpler setup without touching GCP or the Workspace Admin console.

## Prerequisites

- A Google Workspace account (DWD is **not** available on free/consumer Gmail).
- Admin access to the Workspace (to approve the DWD scopes).
- A user in the Workspace that the service account will impersonate as the sender (e.g. `devbrain@yourdomain.com`).

## Setup Steps

1. **Create a GCP project** at <https://console.cloud.google.com/> (or reuse an existing one).
2. **Enable the Gmail API** for that project: APIs & Services → Library → "Gmail API" → Enable.
3. **Create a service account**: IAM & Admin → Service Accounts → Create. Give it a name like `devbrain-notifications`. No project roles are required.
4. **Enable domain-wide delegation** on the service account: open the service account → Details → "Show domain-wide delegation" → check **Enable G Suite Domain-wide Delegation**.
5. **Download a JSON key**: Keys tab → Add Key → Create new key → JSON. Save the file securely.
6. **Authorize the scope in Workspace Admin**: go to <https://admin.google.com/> → Security → Access and data control → API controls → Domain-wide delegation → **Add new**. Paste the service account's **Client ID** (numeric) and add the scope:
   ```
   https://www.googleapis.com/auth/gmail.send
   ```
7. **Create (or pick) a sender user** in Workspace — for example `devbrain@yourdomain.com`. This is the `sender_email` that the service account will impersonate.
8. **Place the credentials JSON** on the DevBrain host at:
   ```
   ~/.devbrain/credentials/gmail-sa.json
   ```
   Make sure it's only readable by your user: `chmod 600 ~/.devbrain/credentials/gmail-sa.json`.

## Configuration

In `config/devbrain.yaml`:

```yaml
notifications:
  gmail_dwd:
    enabled: true
    credentials_path: ~/.devbrain/credentials/gmail-sa.json
    sender_email: devbrain@yourdomain.com
```

## Registration

```bash
devbrain register --channel gmail_dwd:you@example.com
```

The address after `gmail_dwd:` is the recipient. Messages will arrive from `sender_email`.

## Troubleshooting

**`unauthorized_client` / `Client is unauthorized to retrieve access tokens`**
: The DWD scope authorization in Workspace Admin is missing or hasn't propagated yet. Double-check the Client ID and scope string, then wait a few minutes.

**`Delegation denied for <sender_email>`**
: The `sender_email` user doesn't exist in your Workspace, or the service account isn't authorized to impersonate them.

**`Gmail API has not been used in project ... or it is disabled`**
: You enabled the API in a different project than the one that owns the service account. Enable it in the correct project.
