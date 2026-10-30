# VyapAI — Go-Live Runbook

Two independent steps. Do Step 1 first (it needs no accounts); Step 2 makes
the app reachable on the public internet.

---

## Step 1 — Put the Telegram bot live (local machine, ~5 min)

1. In Telegram, message **@BotFather** → `/newbot` → pick name + username
   (ends in `bot`). Copy the token it gives you.
2. Edit `vyapai/.env` and add **(never paste a token into chat)**:

   ```
   TELEGRAM_BOT_TOKEN=<token from BotFather>
   TELEGRAM_SECRET_TOKEN=<random string, e.g. one you generate>
   ```

   Generate the secret: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
3. Restart the server (ask the agent: "restart vyapai").
4. Dashboard → **Channels → Link via /start code**. This time click the
   one-tap **"Open in Telegram"** link and send `/start <code>` to your
   REAL bot. The card flips to **Ready** with the bot's @username shown.

Live bot features unlocked: login OTPs in chat, `/help` · `/status`,
text + voice-note + photo orders, 💵/✅ payment buttons, reminders.

On the hosted deployment (Step 2), register the webhook once the public
URL exists:

```
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://<your-app-url>/webhooks/telegram" \
  -d "secret_token=$TELEGRAM_SECRET_TOKEN" \
  -d 'allowed_updates=["message","callback_query"]'
```

Verify with `getWebhookInfo`; delete with `deleteWebhook` if you go back
to polling (the two are mutually exclusive).

---

## Step 2 — Deploy publicly on free tiers (~10 min)

Chosen stack (validated in docs/ZERO_COST_BLUEPRINT.md):

| Piece | Service | Plan | Why |
|---|---|---|---|
| App | Render (Docker web service) | Free | zero-ops; sleeps ~15 min idle, wakes in ~60 s |
| DB | Neon Postgres | Free | real Postgres; no 30-day expiry, unlike Render/Koyeb DBs |
| Optional later | Cloudflare R2 (media), Upstash Redis | Free | durable media / cache |

### Steps

1. **Neon** — sign up at https://neon.com → create project "vyapai" →
   copy the connection string. Change the scheme to
   `postgresql+psycopg2://...` (SQLAlchemy driver inline).
2. **Render** — sign up at https://render.com → **New → Blueprint** →
   point it at your GitHub fork/clone of this repo (contains
   [render.yaml](../render.yaml)). When prompted, paste the Neon string
   as the `DATABASE_URL` value. Other env vars are pre-filled.
   (No GitHub? New Web Service → Runtime **Docker** → same repo → set the
   env vars from render.yaml manually.)
3. First deploy: Render builds the Dockerfile and boots; tables are
   created automatically (`APP_ENV=development` → `init_db()`).
   Check `https://<app>.onrender.com/health` → `{"status":"ok"}` and the
   dashboard at `/`.
4. Register your shop on the live URL (onboarding form) and repeat
   Step 1's pairing against the live bot if you want the bot served from
   Render (set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_SECRET_TOKEN` env vars in
   the dashboard, then run the `setWebhook` curl above).
5. Update BotFather's `/setcommands` menu so `/help · /status · /start`
   appear in the bot's UI.

### Honest limits of the free-tier deploy

- Render free **sleeps**; first request after idle takes ~60 s.
- Render free disk is **ephemeral** — voice notes/photos (local media
  backend) vanish on redeploy; add the R2 env vars for durable media.
- `APP_ENV=development` here means the production WhatsApp-signature
  guard is **off** (there is no WhatsApp number yet anyway) and Postgres
  schema is created by `init_db()` instead of Alembic. Before real money
  flows: set `APP_ENV=production`, run `alembic upgrade head` as the
  service's build command, and pass `WHATSAPP_APP_SECRET` (see
  [COMPLIANCE_CHECKLIST.md](COMPLIANCE_CHECKLIST.md)).
- Secrets belong in Render's env vars — never in git or chat.
