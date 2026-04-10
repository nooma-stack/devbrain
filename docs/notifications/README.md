# DevBrain Notifications

DevBrain sends factory job notifications through configured channels so you always know when a job is ready for review, has failed, or needs human attention.

## Overview

Notifications are dispatched by the factory whenever an interesting event occurs during a job's lifecycle. You can subscribe to one or more channels, and each event can be routed to one or more of your registered destinations.

## Supported Channels

| Channel | Description | Setup Guide |
|---------|-------------|-------------|
| `tmux` | Send messages to an active tmux session's status line | [tmux.md](tmux.md) |
| `smtp` | Email via any SMTP provider (Gmail, Fastmail, Mailgun, SES, etc.) | [smtp.md](smtp.md) |
| `gmail_dwd` | Gmail send via Google Workspace domain-wide delegation | [gmail-dwd.md](gmail-dwd.md) |
| `gchat_dwd` | Google Chat direct messages via domain-wide delegation | [gchat-dwd.md](gchat-dwd.md) |
| `telegram` | Telegram bot direct messages | [telegram.md](telegram.md) |
| `webhook_slack` | Slack incoming webhooks | [webhooks.md](webhooks.md) |
| `webhook_discord` | Discord channel webhooks | [webhooks.md](webhooks.md) |
| `webhook_generic` | Any HTTP endpoint that accepts JSON POSTs | [webhooks.md](webhooks.md) |

## Quick Start

1. **Enable channels** in `config/devbrain.yaml` (see each channel's setup guide).
2. **Register yourself** with one or more channels:
   ```bash
   devbrain register \
     --channel tmux:work \
     --channel smtp:you@example.com \
     --channel webhook_slack:https://hooks.slack.com/services/XXX/YYY/ZZZ
   ```
3. **Done.** The next factory event that matches your subscriptions will reach you.

## Event Types

DevBrain fires notifications for the following events:

| Event | When It Fires |
|-------|---------------|
| `job_ready` | A factory job has finished and is ready for human review |
| `job_failed` | A factory job failed during planning or execution |
| `lock_conflict` | A job could not start because another job holds a conflicting lock |
| `unblocked` | A previously blocked job has been unblocked and can proceed |
| `needs_human` | A job is paused waiting on a human decision (approval, disambiguation, etc.) |

## Configuration File

All channel toggles live in `config/devbrain.yaml` under the `notifications:` key. Secrets (SMTP passwords, bot tokens, etc.) should be supplied via environment variables where possible. See the per-channel guides for full examples.

## Troubleshooting

- Run `devbrain notifications test` to send a test message through every registered channel.
- Check `~/.devbrain/logs/notifications.log` for dispatch errors.
- Verify your registration with `devbrain register --list`.
