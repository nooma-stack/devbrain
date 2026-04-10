# tmux Channel

Deliver factory notifications directly to a tmux session's status line (or as a `display-message` popup). Great for heads-up alerts while you're already working in a terminal.

## Prerequisites

- `tmux` installed. Most Unix-like systems ship with it; on macOS: `brew install tmux`; on Debian/Ubuntu: `sudo apt install tmux`.
- At least one named tmux session you plan to keep running while you want notifications.

Verify tmux is available:

```bash
tmux -V
```

## Configuration

Enable the tmux channel in `config/devbrain.yaml`:

```yaml
notifications:
  tmux:
    enabled: true
    # How the message is shown: "display" (popup) or "status" (status line)
    mode: display
    # How long status-line messages stay visible (milliseconds)
    display_time_ms: 5000
```

No secrets are needed. The channel uses the `tmux` binary on your `PATH`.

## Registration

Register yourself, specifying the tmux session name that should receive messages:

```bash
devbrain register --channel tmux:<your-session-name>
```

Example:

```bash
devbrain register --channel tmux:work
```

You can find your session name with `tmux list-sessions`.

## Delivery Notes

- **The target session must be running** when the notification fires. DevBrain sends messages via `tmux display-message -t <session>`; if the session does not exist, the dispatch is logged and skipped.
- The session does **not** need to be attached to a terminal — a detached session is fine, but messages in a detached session's status line may be missed until you reattach.
- If you use multiple sessions, register once per session name you want to notify.

## Troubleshooting

**"No active tmux session"**
: The session name you registered does not exist right now. Run `tmux list-sessions` to see live sessions and either start the expected one or re-register with `devbrain register --channel tmux:<current-name>`.

**Messages flash by too quickly**
: Increase `display_time_ms` in `config/devbrain.yaml`, or switch `mode` to `status` so messages land in the status line instead of as a popup.

**Nothing happens and no errors**
: Check `~/.devbrain/logs/notifications.log`. Make sure the `devbrain` process can see the same `tmux` server (same user, same socket) as your session.
