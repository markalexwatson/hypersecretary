This is where you put information the secretary should know. Drop markdown files in this directory and they'll be loaded automatically at startup.

Examples of useful context files:

- `team.md` — your direct reports, their roles, what they're working on
- `calendar_this_week.md` — synced from Google Calendar (manually or via cron)
- `priorities.md` — current quarter OKRs, top-of-mind items
- `comms_style.md` — examples of how you write, preferred phrases, things to avoid

The bot loads all .md files from this directory alphabetically and includes them in the system prompt sent to both models.

Keep files concise. Every token here is sent with every message, so don't dump entire documents — summarise what the AI actually needs to know.
