# Deploy — Adstail creative bot

The bot uses **long polling**, so it just needs a process that runs 24/7.
No inbound HTTP, no domain. Below: a recommended path (Railway) and an
alternative (Fly.io). Both build straight from the included `Dockerfile`.

---

## 0. Before anything: rotate the token

The current token was pasted in a chat — treat it as compromised.

1. Open **@BotFather** in Telegram → `/revoke` → pick the bot → get a NEW token.
2. You'll paste that new token into the platform as `BOT_TOKEN` (step below).
   Never commit it to git.

---

## Option A — Railway (simplest for bots)

Railway runs a long-lived worker happily. Free trial credit to start; after
that it's a low hobby tier. A card is required at the deploy step (anti-fraud),
even on the trial — that part is on you, I can't enter it.

### A1. Put the code on GitHub
From the project folder:

```bash
cd "/Users/ekaterinaarseneva/test bot"
git init
git add bot.py requirements.txt Dockerfile Procfile .env.example .gitignore .dockerignore
git commit -m "Adstail creative bot — deploy"
```

Create an empty repo on github.com (private is fine), then:

```bash
git remote add origin git@github.com:<your-user>/adstail-creative-bot.git
git branch -M main
git push -u origin main
```

### A2. Deploy
1. Go to **railway.app** → sign in with GitHub (use arseneva.katerina@gmail.com / GitHub).
2. **New Project → Deploy from GitHub repo** → pick the repo.
3. Railway detects the `Dockerfile` and builds automatically.
4. Open the service → **Variables** → add:
   - `BOT_TOKEN` = the new token from BotFather
   - (optional) `TARGET_CHAT_ID` = where uploads should post
5. It redeploys. Open **Deploy Logs** — you want to see:
   `Run polling for bot @Kate_preview_bot`.

Done — the bot is now online independent of your laptop.

---

## Option B — Fly.io (also free allowance, needs a card)

```bash
# one-time
brew install flyctl
fly auth signup        # or: fly auth login

cd "/Users/ekaterinaarseneva/test bot"
fly launch --no-deploy # detects Dockerfile; say NO to databases/Postgres
fly secrets set BOT_TOKEN=<new-token>
# optional: fly secrets set TARGET_CHAT_ID=<id>
fly deploy
fly logs               # confirm "Run polling..."
```

In `fly.toml`, since there's no HTTP server, remove the `[http_service]`
block if `fly launch` added one (a polling bot has no port to serve).

---

## Notes / gotchas

- **Only ONE instance may poll a token at a time.** If two run (e.g. your
  laptop + the cloud), Telegram returns 409 Conflict. So once it's deployed,
  stop the local copy: `pkill -f bot.py`.
- **State is in-memory.** A redeploy/restart clears all in-progress drafts.
  That's expected for this prototype.
- **No persistence, no auth.** Anyone with the bot link can use it.

## What I can do once you've created the account
Ping me after the first deploy and paste the log line (or any error). I'll
read it, fix config, and confirm polling is healthy.
