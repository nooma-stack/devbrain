# Telegram Channel

Deliver factory notifications as Telegram direct messages from a bot you control. Good option for mobile notifications without any paid service.

## Prerequisites

- A Telegram account (free).
- The Telegram app installed somewhere you can DM a bot.

## Setup Steps

1. **Create a bot** via [@BotFather](https://t.me/BotFather):
   - Open Telegram, search for `@BotFather`, start a chat.
   - Send `/newbot` and follow the prompts:
     - Give the bot a display name (e.g. `DevBrain Notifier`).
     - Give it a username ending in `bot` (e.g. `your_devbrain_bot`).
   - BotFather will reply with an **HTTP API token** that looks like `123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`. **Save this token.**
2. **Record the bot username** (optional but handy for later — it's the `@your_devbrain_bot` handle BotFather confirmed).
3. **Add the token to DevBrain** in `config/devbrain.yaml`:
   ```yaml
   notifications:
     telegram:
       enabled: true
       # Preferred: read from environment variable
       token_env: TELEGRAM_BOT_TOKEN
       # Or, less preferred, inline:
       # token: "123456789:ABC-DEF..."
   ```
   Then export the token:
   ```bash
   export TELEGRAM_BOT_TOKEN="123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
   ```
4. **DM the bot once.** Open Telegram, search for your bot's username, press Start, and send any message (e.g. `hi`). This is mandatory — Telegram won't let a bot message you until you've messaged it first.
5. **Run auto-discovery**:
   ```bash
   devbrain telegram-discover
   ```
   This walks you through finding your chat ID, registers the channel for you, and sends a test message to confirm everything works.

## How It Works

The Telegram bot API doesn't expose a "list of users who've messaged me" endpoint. Instead, DevBrain polls [`getUpdates`](https://core.telegram.org/bots/api#getupdates) to find recent private chats with the bot, lists the users it finds, and lets you pick yourself. Once registered, it stores the numeric `chat_id` — future notifications use that directly and no polling is needed.

## Manual Registration

If you already know your chat ID (for example, you've used the bot from a script before), you can skip discovery:

```bash
devbrain register --channel telegram:<your-chat-id>
```

## Troubleshooting

**"Could not find your chat" during `telegram-discover`**
: You haven't messaged the bot yet. Open Telegram, find the bot by its username, press Start, send any message, then rerun `devbrain telegram-discover`.

**"Unauthorized" / `401` from Telegram**
: The token is wrong or revoked. Double-check the `TELEGRAM_BOT_TOKEN` env var, or ask BotFather for a fresh token with `/token`.

**No messages arriving after setup**
: Make sure you haven't blocked the bot in Telegram, and check `~/.devbrain/logs/notifications.log` for dispatch errors.

**Bot found but wrong account picked**
: Re-run `devbrain telegram-discover` and pick the correct user, or re-register manually with the right chat ID.
