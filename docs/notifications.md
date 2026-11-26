# Notifications

SOVA supports multiple notification backends for agent handoffs and system events:

```toml
[notification]
# Desktop notifications (macOS via terminal-notifier or JXA, Linux via notify-send)
desktop = false

# Slack webhook
slack_webhook_url = ""

# Email via SMTP
email_enabled = true
email_to = "dev@example.com"
email_from = "sova@example.com"
email_smtp_host = "smtp.example.com"
email_smtp_port = 587
email_smtp_starttls = true
# Credentials via env: SOVA_NOTIFICATION_EMAIL_SMTP_USER, SOVA_NOTIFICATION_EMAIL_SMTP_PASSWORD

# Generic webhook (HTTP POST)
webhook_url = "https://hooks.example.com/sova"
webhook_headers = '{"Authorization": "Bearer token123"}'  # JSON object with custom headers
# The webhook payload also carries kind, issue (a string, or null), pr_number, run_id and url (null without link_base_url)

# Self-hosted ntfy server (skipped when ntfy_url or ntfy_topic is empty)
ntfy_url = "https://ntfy.example.com"
ntfy_topic = "sova"
ntfy_events = ["needs_human", "failed"]   # kinds sent to ntfy: needs_human, failed, finished, info
link_base_url = "https://sova.example.com"  # notification link: /work/<id>, else /work
# Bearer token: sova config set notification.ntfy_token <token> (stored in the OS keyring when available);
# the SOVA_NOTIFICATION_NTFY_TOKEN environment variable overrides the stored token
# SOVA_NOTIFICATION_NTFY_EVENTS takes comma-separated text or a JSON array
```

Every notification has a kind (`needs_human`, `failed`, `finished` or `info`). Only ntfy filters by kind
(`ntfy_events`); the other backends receive every event. Entries in `ntfy_events` are trimmed and lowercased,
and an unknown or malformed entry is ignored with a warning rather than failing config load. A run that is
waiting on you sends one `needs_human` push; the exit-time summary for it (and for a step failure the engine
already reported, or a finish the engine already reported) is `info`, so the default filter yields exactly one push
and opting into `finished` does not double it. An ntfy push is titled with the event ("Developer failed #12") so a
lock screen reads at a glance; its body holds the project and the detail.

Point ntfy at a self-hosted server. Topics on the hosted ntfy.sh are public (anyone who guesses the topic name
can read it) and are not end-to-end encrypted, so issue titles, step errors and dashboard links would leave
your network.

All backends are fire-and-forget. Errors are logged but never block agent execution.
