# Hypersecretary

A Telegram bot that routes messages to Gemini Flash (fast/cheap) or Claude (deep reasoning). Type normally for Flash, prefix with `/claude` when you need the big brain.

## Quick start (local)

```bash
# 1. Clone / copy these files to your machine

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up your keys
cp .env.example .env
# Edit .env with your actual tokens (see below)

# 4. Run
python bot.py
```

## Getting your tokens

### Telegram bot
1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow the prompts
3. Copy the token into `.env`
4. Message [@userinfobot](https://t.me/userinfobot) to get your user ID for `ALLOWED_USERS`

### Anthropic API key
1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create an API key
3. Copy into `.env`

### Google AI API key
1. Go to [aistudio.google.com](https://aistudio.google.com)
2. Get an API key
3. Copy into `.env`

## Deploy to Fly.io (free tier)

```bash
# 1. Install the Fly CLI
curl -L https://fly.io/install.sh | sh

# 2. Sign up / log in
fly auth signup   # or: fly auth login

# 3. From the project directory, launch the app
fly launch
# It'll detect the Dockerfile and fly.toml. Say yes to the defaults.
# Say NO to setting up a Postgres database — you don't need one.

# 4. Set your secrets (these are your .env variables, stored encrypted by Fly)
fly secrets set TELEGRAM_TOKEN=your-telegram-bot-token
fly secrets set ANTHROPIC_API_KEY=sk-ant-your-key-here
fly secrets set GOOGLE_API_KEY=your-google-ai-key
fly secrets set ALLOWED_USERS=your-telegram-user-id

# 5. Deploy
fly deploy

# 6. Ensure only one machine is running (Fly defaults to two — the bot
#    can only have one instance polling Telegram at a time)
fly scale count 1

# 7. Check it's running
fly status
fly logs   # tail the logs to see it start up
```

### Updating

After editing files locally (system prompt, context files, bot code):

```bash
fly deploy   # rebuilds and redeploys in ~30 seconds
```

### Useful Fly commands

```bash
fly logs              # live logs
fly ssh console       # SSH into the container
fly secrets list      # see what's set (values hidden)
fly apps restart      # restart if something's stuck
```

## Usage

| Command | What happens |
|---|---|
| Just type | Goes to Gemini Flash ⚡ |
| `/claude <message>` | Goes to Claude 🟠 |
| `/inbox` | Show all recent items |
| `/inbox email` | Show only emails |
| `/inbox calendar` | Show only calendar events |
| `/search <keyword>` | Search inbox by keyword |
| `/ask <question>` | Ask Flash a question about your inbox |
| `/do <action> [args]` | Trigger an outbound action |
| `/actions` | List available actions |
| `/clear` | Resets conversation history |
| `/status` | Shows models, inbox counts |
| `/help` | Shows commands |

### Item types

Notifications are categorised by type, each with its own icon:

📧 email · 📅 calendar · 🚨 alert · ✅ task · 💰 payment · 📰 news · 🚀 deploy · ⏰ reminder · 🦋 bluesky · 🐘 mastodon · 📌 other

## Adding context

Drop `.md` files in the `context/` directory. These are loaded at startup and included in the system prompt for both models. Useful for:

- Team info and org structure
- Current priorities and OKRs  
- Calendar summaries
- Communication style notes

**Restart the bot after adding context files** (locally: restart python, Fly: `fly deploy`).

## Setting up email ingestion

This lets you receive emails at a dedicated address (e.g. `hypersecretary@yourdomain.com`) and query them from Telegram.

### 1. Generate a shared secret

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Use this as `WEBHOOK_SECRET` in both your bot's Fly secrets and the Cloudflare Worker.

### 2. Set the secret on Fly

```bash
fly secrets set WEBHOOK_SECRET=your-generated-secret
```

### 3. Deploy the Cloudflare Email Worker

```bash
cd email-worker
npm install

# Set the worker's secrets
npx wrangler secret put WEBHOOK_URL
# → enter: https://your-app-name.fly.dev/webhook/email

npx wrangler secret put WEBHOOK_SECRET
# → enter: the EXACT same value you set on Fly in step 2

# Deploy the worker
npx wrangler deploy

# Verify secrets are set
npx wrangler secret list
# Should show WEBHOOK_URL and WEBHOOK_SECRET
```

**Important:** The `WEBHOOK_SECRET` must be identical on both Fly and the Cloudflare Worker. If emails are being "Dropped" in Cloudflare's Email Routing dashboard, mismatched secrets are the most likely cause.

### 4. Connect it to your email address in Cloudflare

1. Go to Cloudflare dashboard → your domain → Email Routing
2. Create a custom address: `hypersecretary@yourdomain.com`
3. Set the destination to: **Email Worker** → `hypersecretary-email`

### 5. Test it

Send an email to your new address. Within a few seconds you should get a Telegram notification, and `/inbox` should show it.

### Email commands in Telegram

| Command | What it does |
|---|---|
| `/inbox` | Show all recent items, mark as read |
| `/inbox email` | Show only emails |
| `/search OpenTable` | Search by keyword |
| `/ask what reservations do I have?` | Ask Flash about your inbox |
| `/ask summarise today's notifications` | Get a digest |

## Connecting Zapier (or anything else)

The bot has a generic webhook at `POST /webhook/notify` that accepts any notification. This is your universal integration point.

### Webhook format

```json
POST https://hypersecretary.fly.dev/webhook/notify
Header: X-Webhook-Secret: your-secret

{
  "type": "calendar",
  "source": "Google Calendar",
  "title": "Board meeting at 2pm",
  "body": "Quarterly review with investors. Agenda attached.",
  "metadata": {"location": "Zoom", "link": "https://..."},
  "notify": true
}
```

Only `title` is required. `type` defaults to "other", `notify` defaults to true.

### Zapier setup

1. Create a Zap with your trigger (Gmail, Google Calendar, Stripe, etc.)
2. Add action: **Webhooks by Zapier → POST**
3. URL: `https://hypersecretary.fly.dev/webhook/notify`
4. Headers: `X-Webhook-Secret: your-secret`
5. Data: Map fields from the trigger to the JSON format above

### Example Zaps

**Google Calendar → new event notification:**
```json
{
  "type": "calendar",
  "source": "Google Calendar",
  "title": "{{Event Title}} at {{Event Start Time}}",
  "body": "{{Event Description}}",
  "metadata": {"location": "{{Event Location}}"}
}
```

**Stripe → payment received:**
```json
{
  "type": "payment",
  "source": "Stripe",
  "title": "Payment received: {{Amount}} from {{Customer Name}}",
  "body": "Invoice {{Invoice ID}}"
}
```

**RSS → industry news:**
```json
{
  "type": "news",
  "source": "{{Feed Title}}",
  "title": "{{Entry Title}}",
  "body": "{{Entry Summary}}",
  "notify": false
}
```

**Bluesky → mentions or replies:**
```json
{
  "type": "bluesky",
  "source": "{{Author Handle}}",
  "title": "{{Author Handle}} replied to your post",
  "body": "{{Post Text}}",
  "metadata": {"uri": "{{Post URI}}"}
}
```

**Mastodon → mentions or boosts:**
```json
{
  "type": "mastodon",
  "source": "{{Account Name}}",
  "title": "{{Account Name}} mentioned you",
  "body": "{{Status Content}}",
  "metadata": {"url": "{{Status URL}}"}
}
```

Set `"notify": false` for high-volume feeds — items are stored but won't ping your phone. You can still see them with `/inbox news` or `/ask any interesting news today?`

### Testing the webhook

```bash
curl -X POST https://hypersecretary.fly.dev/webhook/notify \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-secret" \
  -d '{"type":"alert","source":"test","title":"Hello from curl"}'
```

## Scheduled tasks

Three ways to run things on a schedule, in order of ease:

### Option A: Zapier Schedule (easiest, no code)

Zapier has a **Schedule by Zapier** trigger that fires at set times. Combine it with the webhook action to create scheduled notifications.

**Morning briefing reminder at 7am:**
1. Trigger: **Schedule by Zapier** → Every Day at 7:00am
2. Action: **Webhooks by Zapier → POST** to `/webhook/notify`
3. Body:
```json
{
  "type": "reminder",
  "source": "Schedule",
  "title": "Morning — check /ask for your briefing",
  "body": "Try: /ask summarise everything that came in overnight"
}
```

**Weekly digest prompt on Friday at 4pm:**
```json
{
  "type": "reminder",
  "source": "Schedule",
  "title": "Weekly review time",
  "body": "Try: /ask summarise this week's inbox by type"
}
```

This is a nudge-based approach — the schedule reminds you, and you ask the bot for the actual summary. Simple, and the bot always has your latest inbox to work from.

### Option B: GitHub Actions (free, more powerful)

For scheduled tasks that need to do actual work — call an API, fetch data, then push results to your bot. Create a `.github/workflows/` file in a **private** repo (to keep secrets safe).

**Example: daily calendar sync**
```yaml
name: Morning briefing
on:
  schedule:
    - cron: '0 7 * * *'  # 7am UTC — adjust for your timezone
  workflow_dispatch:       # allows manual trigger for testing

jobs:
  briefing:
    runs-on: ubuntu-latest
    steps:
      - name: Send briefing prompt
        run: |
          curl -X POST "${{ secrets.WEBHOOK_URL }}/webhook/notify" \
            -H "Content-Type: application/json" \
            -H "X-Webhook-Secret: ${{ secrets.WEBHOOK_SECRET }}" \
            -d '{
              "type": "reminder",
              "source": "GitHub Actions",
              "title": "Good morning — your daily briefing is ready",
              "body": "Inbox summary available via /ask"
            }'
```

You can make this more sophisticated by calling external APIs (Google Calendar, weather, news) in earlier steps and including the results in the body.

### Option C: Cron inside the bot (most integrated, most code)

Add an async background task to `bot.py` that runs on a schedule using `asyncio`. This can directly query the inbox, call Flash, and send the result to Telegram — a fully automated briefing with no manual prompt needed. This is the most powerful option but means more code to maintain.

## Outbound actions

The bot can trigger external webhooks (Zapier, IFTTT, anything) via the `/do` command. This turns your Telegram into a remote control for anything with a webhook URL.

### Setup

Copy the example and add your own actions:

```bash
cp actions.example.json actions.json
```

Edit `actions.json` with your webhook URLs:

```json
{
  "lights_off": {
    "url": "https://maker.ifttt.com/trigger/lights_off/with/key/YOUR_KEY",
    "description": "Turn off living room lights"
  },
  "tweet": {
    "url": "https://hooks.zapier.com/hooks/catch/123456/abcdef/",
    "description": "Post to Twitter/X",
    "fields": ["status"]
  },
  "log_mood": {
    "url": "https://hooks.zapier.com/hooks/catch/123456/ghijkl/",
    "description": "Log mood score to spreadsheet",
    "fields": ["score", "note"]
  }
}
```

Redeploy after editing: `fly deploy`

### Usage in Telegram

```
/actions              → list what's available
/do lights_off        → no args needed
/do tweet Just shipped the new feature!
/do log_mood 8 great day, got the bot working
```

### Config options

| Field | Required | Description |
|---|---|---|
| `url` | yes | Webhook URL |
| `description` | no | Shown in `/actions` list |
| `fields` | no | Named args, split by spaces. Last field gets the remainder |
| `method` | no | HTTP method, default `POST` |
| `headers` | no | Extra headers as `{"key": "value"}` |
| `body_template` | no | Static JSON merged with args |

### IFTTT Maker Webhooks

For IFTTT, the URL format is `https://maker.ifttt.com/trigger/{event}/with/key/{key}`. If you don't specify `fields`, any args are automatically mapped to `value1`, `value2`, `value3` (IFTTT's convention).

### Zapier Webhooks

In Zapier, create a Zap with trigger **Webhooks by Zapier → Catch Hook**. It'll give you a URL. Map the incoming fields (from `fields` in your config) to whatever action you want.

### AI-orchestrated actions

Both Gemini Flash and Claude can trigger actions autonomously during normal conversation. You don't need to use `/do` — just ask naturally:

```
"Turn off the lights"
"Post 'just shipped v2' to mastodon and log my mood as 9"
"Remind me to check the heating"
```

The models see your available actions in their system prompt and embed action tags in their responses. The bot executes them before you see the reply.

**Security:** Actions are only available during direct conversation. The `/ask` command (which processes untrusted inbox content like emails and notifications) runs in safe mode with actions disabled. This prevents prompt injection — a malicious email containing "turn off the lights" can't trigger anything when you ask the bot to summarise your inbox.

## Social notifications (Mastodon & Bluesky)

Neither platform supports outbound webhooks for notifications, so the bot includes a polling script that checks both APIs and forwards new mentions, replies, likes, boosts, and follows to your Telegram inbox.

### 1. Get your credentials

**Mastodon:**
1. Go to your instance's web UI → Preferences → Development → New Application
2. Name it anything (e.g. "hypersecretary")
3. Uncheck everything except `read:notifications`
4. Save, then copy the access token

**Bluesky:**
1. Go to Settings → App Passwords → Add App Password
2. Name it anything, copy the generated password

### 2. Run locally (one-off test)

```bash
export WEBHOOK_URL=https://hypersecretary.fly.dev
export WEBHOOK_SECRET=your-secret
export MASTODON_INSTANCE=https://mastodon.social
export MASTODON_TOKEN=your-token
export BLUESKY_HANDLE=yourname.bsky.social
export BLUESKY_PASSWORD=your-app-password

pip install requests python-dotenv
python social_poller.py
```

### 3. Run on a schedule (GitHub Actions, free)

Push the repo to a **private** GitHub repo (to keep secrets safe), then add these secrets in Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `WEBHOOK_URL` | `https://hypersecretary.fly.dev` |
| `WEBHOOK_SECRET` | your webhook secret |
| `MASTODON_INSTANCE` | `https://mastodon.social` (or your instance) |
| `MASTODON_TOKEN` | your Mastodon access token |
| `BLUESKY_HANDLE` | `yourname.bsky.social` |
| `BLUESKY_PASSWORD` | your Bluesky app password |

The included workflow (`.github/workflows/social_poll.yml`) runs every 5 minutes and uses GitHub Actions cache to remember which notifications it has already forwarded. Either platform can be left unconfigured — the poller will skip it.

### What you'll see in Telegram

```
🐘 @someone@mastodon.social mentioned you
🐘 @someone@mastodon.social boosted your post
🦋 @someone.bsky.social replied to you
🦋 @someone.bsky.social liked your post
```

Use `/inbox mastodon` or `/inbox bluesky` to filter, or `/ask any interesting social activity today?` for a summary.

## Architecture

```
You (Telegram) → Bot (Fly.io) → Gemini Flash API  (default)
                               → Claude API        (/claude prefix)
                               ← context/*.md      (loaded at startup)
                               ← SQLite inbox      (/inbox, /search, /ask)

Cloudflare Email Worker → POST /webhook/email → inbox (type: email)
Zapier / scripts / anything → POST /webhook/notify → inbox (any type)
GitHub Actions (cron) → social_poller.py → POST /webhook/notify → inbox (mastodon/bluesky)
```

~500 lines of Python + a small Cloudflare Worker + a polling script. Hosting cost: £0.
