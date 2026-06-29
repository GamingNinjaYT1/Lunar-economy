# Lunar Economy Telegram Bot — Setup Guide (GitHub + systemd)

## Files in this repo
- `bot.py` — the bot (reads secrets from environment variables — safe to commit to GitHub)
- `requirements.txt` — Python dependencies
- `LunarEconomy.service` — systemd service template (holds your real token/ID locally — do NOT commit the filled-in version to a public repo)

## Why this setup is GitHub-safe
`bot.py` no longer has your token or ID hardcoded — it reads them from environment
variables (`BOT_TOKEN`, `ADMIN_IDS`) at runtime. Those variables live only in
`LunarEconomy.service` on your VPS, which never has to be pushed to GitHub.
You can safely make your GitHub repo public with just `bot.py` and
`requirements.txt` in it.

**Important:** Keep the filled-in real version of `LunarEconomy.service` out of
your public repo (or add it to `.gitignore`). Never commit your real `BOT_TOKEN`.

## 1. Get a bot token
Message @BotFather on Telegram → `/newbot` → follow prompts → copy the token.

## 2. Your admin Telegram ID
Already set for you: `7140576750`. For more admins later, use a comma-separated
list, e.g. `7140576750,111222333`.

## 3. SSH into your VPS
```bash
ssh youruser@your_vps_ip
```

## 4. Clone your repo
```bash
git clone https://github.com/GamingNinjaYT1/your-repo-name.git lunareconomy
cd lunareconomy
```

## 5. Install Python tools
```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv -y
```

## 6. Create and activate a virtual environment
```bash
python3 -m venv venv
source venv/bin/activate
```

## 7. Install dependencies
```bash
pip install -r requirements.txt
```

## 8. Test it manually first (quick token check)
```bash
BOT_TOKEN=your_real_token_here ADMIN_IDS=7140576750 python3 bot.py
```
Send `/start` to your bot in Telegram to confirm it replies. Then `Ctrl+C`.

## 9. Find your actual VPS Linux username
```bash
whoami
```
This is NOT your Telegram @handle — it's something like `root`, `ubuntu`, or
a custom name set when the VPS was created. You'll need this in step 10.

## 10. Set up the systemd service
```bash
sudo nano /etc/systemd/system/LunarEconomy.service
```
Paste the contents of `LunarEconomy.service`, then replace:
- `REPLACE_WITH_YOUR_VPS_USERNAME` → output of `whoami` (appears 3 times)
- `PUT_YOUR_BOTFATHER_TOKEN_HERE` → your real bot token from step 1

`ADMIN_IDS=7140576750` is already correct — leave it as is.

Save: `Ctrl+O`, Enter, `Ctrl+X`.

## 11. Enable and start it
```bash
sudo systemctl daemon-reload
sudo systemctl enable LunarEconomy
sudo systemctl start LunarEconomy
```

## 12. Verify it's running
```bash
sudo systemctl status LunarEconomy
```
Should say "active (running)".

## 13. Watch logs if something's wrong
```bash
sudo journalctl -u LunarEconomy -f
```
`Ctrl+C` to stop watching (bot keeps running).

## 14. Add the bot to your group
- Add it as a member
- Promote to **admin** with "Restrict members" permission (needed for
  `/kill`, spam auto-mute, etc.)

## 15. Updating the bot later
```bash
cd /home/youruser/lunareconomy
git pull
sudo systemctl restart LunarEconomy
```
Since the token/ID live in the service file (not in `bot.py`), pulling
updates from GitHub never touches or exposes your secrets.

## 16. First commands to try
```
/start          → full command list
/daily          → claim starter coins + streak bonus
/work           → earn coins every 30 min
/shop           → cosmetic items
/profile        → your stats card
```
Hidden admin commands (only respond to ID `7140576750`):
`/addcoins`, `/setbal`, `/ban`, `/unban`, `/resetuser`, `/broadcast`,
`/spamtoggle`, `/spamset`, `/spamstatus`

## Notes
- `lunar_economy.db` is created automatically in the project folder — back it
  up before any redeploy if you don't want to lose balances.
- This bot is virtual-coins only — no real-money deposit/withdraw.
