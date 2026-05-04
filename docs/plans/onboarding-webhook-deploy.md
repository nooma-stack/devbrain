# Onboarding Webhook — Deploy Notes

One-time admin setup to expose the host-native `devbrain-onboard`
service (running on the Mac Studio at `127.0.0.1:8000`) at the public
URL `https://devbrain.lighthouse-therapy.com/onboard/*`.

The dev's onboarding kit `.md` file embeds URLs of the form
`https://devbrain.lighthouse-therapy.com/onboard/<token>/pubkey` etc. —
the dev's AI agent (or the dev themselves) hits those URLs with their
pubkey and OAuth token. Without the steps below, those URLs resolve to
nothing and onboarding stalls.

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
                                                                          launchd service)
```

Why host-native (not Docker): the service is a 280-line stdlib
http.server. Docker added a `credsStore` keychain dependency that's
hostile to SSH-only operation, and bought us nothing in return.
Running under launchd directly with the existing devbrain venv is
simpler, faster to iterate, and easier to debug.

## Tahoe TCC platform issue — resolved (2026-05-04)

**Original symptom (2026-05-01):** Python 3.14 + macOS Tahoe + launchd
dropped `http.server`'s listening socket into CLOSED state immediately
after bind. Diagnosed as macOS 26 (Tahoe) silently denying the new
"Local Network" TCC permission to faceless launchd processes (no UI to
display the consent prompt). Reproduced with every `http.server`
variant we tried (ThreadingHTTPServer, plain HTTPServer, bare
`python -m http.server`, nohup/setsid/launchctl-asuser) — all bound
and immediately closed. **Worked only when run interactively in an
attached TTY** because TTY-attached processes inherit the GUI user's
TCC grants.

**Resolution:** `launchd/com.devbrain.onboard.plist` was updated to
tunnel the program through `/usr/bin/login -fpq` and set
`SessionCreate=true`, both of which place the spawned process inside
the GUI user's Aqua session context. The session inherits the user's
existing Local Network TCC grant, so http.server's bind survives and
the socket actually listens.

**Verified working** on 2026-05-04 with Mac Studio Tahoe + Python 3.14
+ devbrain venv:
- `launchctl load -w` succeeds
- Python process binds 127.0.0.1:8000 in LISTEN state
- `curl http://127.0.0.1:8000/healthz` returns `{"status":"ok"}`
- Survives launchd respawn after kill -9

**No follow-up investigation needed.** The original "try Python 3.13 /
3.12, file against Python upstream" leads were chasing the wrong
problem — it wasn't a Python http.server bug, it was a macOS TCC
deny. The plist workaround is sufficient and stable.

## Step 1 — Smoke-test the webhook on the Mac Studio

In an active Terminal.app session on the Mac Studio (RDP'd or local):

```bash
cd ~/devbrain
./bin/devbrain-onboard
# Expect: "Starting devbrain-onboard on 127.0.0.1:8000"
# Leaves the process in foreground — keep this terminal open or
# attach via tmux for persistence (see workaround above).

# In another terminal window:
curl -s http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

## Step 2 — Install the launchd plist

Copy `com.devbrain.onboard.plist` to
`~/Library/LaunchAgents/com.devbrain.onboard.plist`, then load it:

```bash
ssh mac-studio
cp ~/devbrain/launchd/com.devbrain.onboard.plist \
   ~/Library/LaunchAgents/com.devbrain.onboard.plist
launchctl load ~/Library/LaunchAgents/com.devbrain.onboard.plist
launchctl list | grep devbrain.onboard
# Expect: a numeric PID, "0", "com.devbrain.onboard"

curl -s http://127.0.0.1:8000/healthz
# {"status":"ok"}

# Logs:
tail -f ~/Library/Logs/devbrain-onboard.log
```

Stop / restart:

```bash
launchctl unload ~/Library/LaunchAgents/com.devbrain.onboard.plist
launchctl load   ~/Library/LaunchAgents/com.devbrain.onboard.plist
```

## Step 3 — DNS

Add an A record for `devbrain.lighthouse-therapy.com` pointing at the
VPS public IP (`72.60.64.155`). Same DNS provider you use for
`n8n.lighthouse-therapy.com` etc. Wait for propagation
(`dig +short devbrain.lighthouse-therapy.com` returns `72.60.64.155`).

## Step 4 — Reverse SSH tunnel (Mac Studio → VPS)

Mac Studio dials out to the VPS and exposes its loopback port 8000 on
the VPS's loopback port 8000. Persistent — relaunches if SSH drops.

### 4a. Provision an SSH key for the tunnel

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

### 4b. SSH config on Mac Studio

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

### 4c. launchd plist for tunnel persistence

Copy `com.devbrain.onboard-tunnel.plist` similarly:

```bash
cp ~/devbrain/launchd/com.devbrain.onboard-tunnel.plist \
   ~/Library/LaunchAgents/com.devbrain.onboard-tunnel.plist
launchctl load ~/Library/LaunchAgents/com.devbrain.onboard-tunnel.plist
launchctl list | grep devbrain
# Expect both com.devbrain.onboard and com.devbrain.onboard-tunnel
```

Verify it stays up:

```bash
ssh root@72.60.64.155 'curl -s http://127.0.0.1:8000/healthz'
# {"status":"ok"}
```

## Step 5 — Traefik route on the VPS

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

## Step 6 — End-to-end smoke test

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
  (replay) or the invitation has expired.
- **502 Bad Gateway** at the public URL: Traefik can't reach
  `localhost:8000`. Either the SSH tunnel is dead (`launchctl list |
  grep tunnel`) or the webhook service is down (`launchctl list |
  grep onboard`). Check `~/Library/Logs/devbrain-onboard.log`.
- **Cert errors**: Traefik letsencrypt provisioning failed. Check
  Traefik logs; usually a DNS propagation issue or rate limit.
