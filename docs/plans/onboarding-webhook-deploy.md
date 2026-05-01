# Onboarding Webhook — Deploy Notes

One-time admin setup to expose the `devbrain-onboard` Docker container
(running on the Mac Studio at `127.0.0.1:8000`) at the public URL
`https://devbrain.lighthouse-therapy.com/onboard/*`.

The dev's onboarding kit `.md` file embeds URLs of the form
`https://devbrain.lighthouse-therapy.com/onboard/<token>/pubkey` etc. —
the dev's AI agent (or the dev themselves) hits those URLs with their
pubkey and OAuth token. Without the steps below, those URLs resolve to
nothing and onboarding stalls.

This is one-time work per DevBrain installation. Once configured,
every new dev onboarding flows through the same path automatically.

## Architecture recap

```
Dev's laptop                  VPS (72.60.64.155)              Mac Studio
─────────────                 ─────────────────                ──────────
agent ─POST─►  https://devbrain.lighthouse-therapy.com/onboard/<token>/...
                                       │
                                       ▼
                               Traefik (existing)
                                       │
                                       ▼ (HTTP proxy, port 8000)
                              localhost:8000 ───[reverse SSH tunnel]──►  127.0.0.1:8000
                                                                         (devbrain-onboard
                                                                          container)
```

## Step 1 — Bring up the container on the Mac Studio

```bash
ssh mac-studio
cd ~/devbrain
docker compose up -d devbrain-onboard
docker compose logs -f devbrain-onboard
# Expect: "Starting devbrain-onboard on 0.0.0.0:8000"
```

Verify the service is reachable on the loopback:

```bash
curl -s http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

## Step 2 — DNS

Add an A record for `devbrain.lighthouse-therapy.com` pointing at the
VPS public IP (`72.60.64.155`). This is the same DNS provider you use
for `n8n.lighthouse-therapy.com` etc. Wait for propagation
(`dig +short devbrain.lighthouse-therapy.com` from anywhere returns
`72.60.64.155`).

## Step 3 — Reverse SSH tunnel (Mac Studio → VPS)

The Mac Studio dials out to the VPS and exposes its loopback port
8000 on the VPS's loopback port 8000. Persistent — relaunches if SSH
drops.

### 3a. Provision an SSH key for the tunnel

On the Mac Studio (as `lhtdev`):

```bash
ssh-keygen -t ed25519 \
  -f ~/.ssh/id_ed25519_devbrain_tunnel \
  -C "devbrain-onboard tunnel from mac-studio" \
  -N ""
cat ~/.ssh/id_ed25519_devbrain_tunnel.pub
```

Copy the public key. Add it to `root@72.60.64.155:~/.ssh/authorized_keys`
restricted to forwarding only:

```
restrict,port-forwarding,permitopen="127.0.0.1:8000" ssh-ed25519 AAAA... devbrain-onboard tunnel from mac-studio
```

The `restrict,port-forwarding,permitopen=...` prefix locks this key
down to creating a single tunnel and nothing else — no shell access,
no other ports.

### 3b. SSH config on Mac Studio

Append to `~lhtdev/.ssh/config`:

```
Host devbrain-tunnel
    HostName 72.60.64.155
    User root
    IdentityFile ~/.ssh/id_ed25519_devbrain_tunnel
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
    ExitOnForwardFailure yes
    RemoteForward 127.0.0.1:8000 127.0.0.1:8000
```

Test once interactively:

```bash
ssh -N devbrain-tunnel &
TUNNEL_PID=$!

# From the VPS:
ssh root@72.60.64.155 'curl -s http://127.0.0.1:8000/healthz'
# {"status":"ok"}

kill $TUNNEL_PID
```

### 3c. launchd plist for persistence

Create `/Users/lhtdev/Library/LaunchAgents/com.devbrain.onboard-tunnel.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTD/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>           <string>com.devbrain.onboard-tunnel</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string>
    <string>-N</string>
    <string>devbrain-tunnel</string>
  </array>
  <key>RunAtLoad</key>       <true/>
  <key>KeepAlive</key>       <true/>
  <key>StandardOutPath</key> <string>/Users/lhtdev/Library/Logs/devbrain-onboard-tunnel.log</string>
  <key>StandardErrorPath</key><string>/Users/lhtdev/Library/Logs/devbrain-onboard-tunnel.log</string>
</dict>
</plist>
```

Load it:

```bash
launchctl load /Users/lhtdev/Library/LaunchAgents/com.devbrain.onboard-tunnel.plist
launchctl list | grep devbrain
```

Verify it stays up across an SSH disconnect:

```bash
ssh root@72.60.64.155 'curl -s http://127.0.0.1:8000/healthz'
# {"status":"ok"}
```

## Step 4 — Traefik route on the VPS

The VPS already runs Traefik fronting n8n + dashboard + forward-auth
(per `~/.claude/CLAUDE.md`). Add a router for the onboard endpoint.

Add to `/root/traefik/dynamic.yml` (or wherever your Traefik dynamic
config lives — adjust path to match the existing setup):

```yaml
http:
  routers:
    devbrain-onboard:
      rule: "Host(`devbrain.lighthouse-therapy.com`) && PathPrefix(`/onboard`)"
      entryPoints:
        - websecure
      service: devbrain-onboard
      tls:
        certResolver: letsencrypt
      # NO forward-auth middleware — these endpoints are token-gated
      # internally; adding OAuth would block the dev's headless agent.

  services:
    devbrain-onboard:
      loadBalancer:
        servers:
          - url: "http://127.0.0.1:8000"
```

Reload Traefik (it watches dynamic.yml so this is usually automatic;
otherwise `docker restart root_traefik_1`).

Verify TLS provisioning:

```bash
curl -v https://devbrain.lighthouse-therapy.com/onboard/dvbn_inv_DEADBEEF/status
# Expect: HTTP/2 404 with body {"status":"error","error":"invalid_token_format"}
# (404 because the token isn't real, but the endpoint is reachable.)
```

## Step 5 — End-to-end smoke test

Stage a test invitation:

```bash
ssh mac-studio
devbrain setup add-dev
# fill in dev_id=onboard-test, name=Test, email=test@brightbot.com
```

Note the path to the kit `.md` file. Open it locally and grab the
`devbrain_invite_token` from the frontmatter (it's also embedded in
the URLs).

POST a dummy pubkey:

```bash
TOKEN="<token from kit>"
curl -X POST -H "Content-Type: application/json" \
  -d '{"pubkey":"ssh-ed25519 AAAATESTING test@brightbot.com"}' \
  "https://devbrain.lighthouse-therapy.com/onboard/$TOKEN/pubkey"
# Expect: {"status":"ok","invitation_status":"pending","pubkey_received":true,"oauth_token_received":false}
```

Check status:

```bash
curl "https://devbrain.lighthouse-therapy.com/onboard/$TOKEN/status"
# Expect: {"status":"ok","invitation_status":"pending","pubkey_received":true,...}
```

POST a dummy oauth token:

```bash
curl -X POST -H "Content-Type: application/json" \
  -d '{"oauth_token":"sk-ant-oat01-TESTING"}' \
  "https://devbrain.lighthouse-therapy.com/onboard/$TOKEN/oauth-token"
# Expect: {"status":"ok","invitation_status":"ready",...}
```

The invitation should now show status=ready in the DB. Once the
reconciler picks it up (Phase 5), it'll auto-activate (assuming
auto_activate=true), wire up authorized_keys + the per-profile dir,
and notify you.

Cleanup the test invitation:

```bash
devbrain setup invitations          # see the row
devbrain setup revoke-invite <id>   # mark revoked (manual cleanup)
devbrain logout --dev onboard-test --yes
```

## Failure modes + diagnostics

- **404 invalid_token_format** at the public URL: token shape is
  wrong. Tokens are `dvbn_inv_<40 hex chars>`.
- **404 not_found** at the public URL: token is valid shape but no
  matching invitation. May have been revoked / expired / never staged.
- **410 invalid_or_expired_or_replayed**: invitation found but in a
  state that rejects this submission. Either the field's already set
  (replay) or the invitation has expired. Stage a new one.
- **502 Bad Gateway** at the public URL: Traefik can't reach
  `localhost:8000`. Either the SSH tunnel is dead (check launchd) or
  the container is down (check `docker compose ps devbrain-onboard`).
- **Cert errors**: Traefik letsencrypt provisioning failed. Check
  Traefik logs; usually a DNS propagation issue or rate limit.
