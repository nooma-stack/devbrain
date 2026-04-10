# SMTP Channel

Send factory notifications as email through any SMTP server. This is the most portable email option — use it with Gmail, Fastmail, Mailgun, AWS SES, or your own mail server.

## Prerequisites

- SMTP credentials from your email provider (host, port, username, password).
- Outbound network access to the SMTP host/port from wherever DevBrain runs.

## Common Providers

### Gmail

- Host: `smtp.gmail.com`
- Port: `587` (STARTTLS) or `465` (SSL)
- Username: your full Gmail address
- Password: **an app password**, not your account password. Create one at <https://myaccount.google.com/apppasswords>. You must have 2-Step Verification enabled on the account.

### Fastmail

- Host: `smtp.fastmail.com`
- Port: `465` (SSL) or `587` (STARTTLS)
- Username: your full Fastmail address
- Password: an app-specific password from Fastmail settings

### Mailgun

- Host: `smtp.mailgun.org` (or `smtp.eu.mailgun.org` for EU)
- Port: `587`
- Username/password: from your Mailgun domain's SMTP credentials page

### AWS SES

- Host: `email-smtp.<region>.amazonaws.com` (e.g. `email-smtp.us-east-1.amazonaws.com`)
- Port: `587`
- Username/password: generate SMTP credentials in the SES console (distinct from your AWS access keys)
- Make sure the sender address is a verified identity in SES

### Generic

Any RFC-compliant SMTP server works. You need the host, port, whether it uses STARTTLS or implicit TLS, and the username/password.

## Configuration

In `config/devbrain.yaml`:

```yaml
notifications:
  smtp:
    enabled: true
    host: smtp.gmail.com
    port: 587
    use_tls: true          # STARTTLS on port 587
    use_ssl: false         # implicit TLS on port 465
    from_address: devbrain@example.com
    # Credentials come from env vars
    username_env: SMTP_USERNAME
    password_env: SMTP_PASSWORD
```

Export the credentials in your environment (or your shell rc file):

```bash
export SMTP_USERNAME="devbrain@example.com"
export SMTP_PASSWORD="your-app-password-here"
```

## Registration

```bash
devbrain register --channel smtp:you@example.com
```

The argument after `smtp:` is the recipient address — notifications will be sent there from `from_address`.

## Troubleshooting

**`535 Authentication failed` (Gmail)**
: You're using your account password instead of an app password. Generate one at <https://myaccount.google.com/apppasswords>.

**`Connection refused` or timeout**
: Your network may block outbound port 587/465. Try the other port, or use a provider on a port your firewall allows.

**Mail is delivered but lands in spam**
: Set `from_address` to an address on a domain with proper SPF/DKIM for your SMTP provider. For SES, the identity must be verified and out of sandbox mode to send to arbitrary recipients.

**`SMTP AUTH extension not supported`**
: Your server doesn't advertise AUTH on that port. Switch to a TLS port (`587` with `use_tls: true`, or `465` with `use_ssl: true`).
