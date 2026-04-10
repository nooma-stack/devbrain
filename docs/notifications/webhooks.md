# Webhook Channels (Slack, Discord, Generic)

DevBrain supports three webhook-style channels that all work the same way: you supply a URL, DevBrain POSTs JSON to it. This document covers all three.

| Channel | URL source |
|---------|------------|
| `webhook_slack` | Slack incoming webhook URL |
| `webhook_discord` | Discord channel webhook URL |
| `webhook_generic` | Any HTTP endpoint that accepts a JSON POST |

## Slack

1. In your Slack workspace, go to **Apps → Manage → Custom Integrations → Incoming Webhooks** (or create a new Slack app and enable "Incoming Webhooks").
2. Click **Add to Slack** and choose the channel where notifications should land.
3. Copy the **Webhook URL** — it looks like `https://hooks.slack.com/services/T000/B000/XXXX`.
4. Register:
   ```bash
   devbrain register --channel webhook_slack:https://hooks.slack.com/services/T000/B000/XXXX
   ```

DevBrain formats messages as Slack-friendly blocks (title as bold header, body as markdown).

## Discord

1. In your Discord server, go to the target channel → **Edit Channel** → **Integrations** → **Webhooks** → **New Webhook**.
2. Give it a name and (optionally) an avatar, then click **Copy Webhook URL**. The URL looks like `https://discord.com/api/webhooks/000/XXXX`.
3. Register:
   ```bash
   devbrain register --channel webhook_discord:https://discord.com/api/webhooks/000/XXXX
   ```

DevBrain uses Discord's `content` + `embeds` format so titles and bodies render nicely.

## Generic

For anything else — ntfy.sh, Microsoft Teams incoming webhooks, a homelab script, an internal dashboard, etc. — use `webhook_generic`. DevBrain POSTs a simple JSON body and expects any 2xx response.

Register:

```bash
devbrain register --channel webhook_generic:https://your.endpoint.example.com/devbrain
```

### Payload Shape

```json
{
  "title": "Job ready for review",
  "body": "Factory job abcd1234 finished planning and is waiting on you.",
  "event_type": "job_ready",
  "timestamp": "2026-04-09T14:23:51Z"
}
```

Fields:

- `title` — short headline, suitable for a subject line or push title.
- `body` — longer message, plaintext or markdown.
- `event_type` — one of `job_ready`, `job_failed`, `lock_conflict`, `unblocked`, `needs_human`.
- `timestamp` — ISO-8601 UTC timestamp of the event.

### Known-compatible targets

- **ntfy.sh**: register with `webhook_generic:https://ntfy.sh/your-topic-name` and subscribe from any ntfy client.
- **Microsoft Teams**: use an "Incoming Webhook" connector URL from the Teams channel connector settings.
- **Custom scripts**: any HTTP server (FastAPI, Flask, Express, a PHP script, etc.) that accepts JSON POSTs.

## Configuration

Webhook channels don't need any config block beyond enabling them:

```yaml
notifications:
  webhook_slack:
    enabled: true
  webhook_discord:
    enabled: true
  webhook_generic:
    enabled: true
```

All destination URLs come from `devbrain register`, not the config file, so you can register multiple URLs per channel type without editing config.

## Troubleshooting

**`404 Not Found`**
: The webhook URL has been revoked (Slack/Discord) or the endpoint path is wrong (generic). Recreate the webhook and re-register.

**`403 Forbidden` from Slack**
: The Slack app owning the webhook was uninstalled from the workspace, or the channel was archived/deleted.

**`400 Bad Request` from Discord**
: Usually means the payload exceeded Discord's size limits (2000 chars for `content`, 6000 across all embeds). DevBrain truncates long bodies, but very long job summaries may still hit limits.

**Generic endpoint returns 2xx but nothing shows up**
: Your receiver accepted the POST but didn't surface it. Check its own logs — DevBrain only knows whether the HTTP call succeeded.

**TLS errors**
: Make sure the target URL uses a valid certificate. Self-signed endpoints aren't supported out of the box.
