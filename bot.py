"""
Lunar Economy style Telegram bot
- Virtual coin economy (no real money)
- Games use Telegram's native emoji dice (send_dice) so outcomes are visibly fair
- Player interaction commands (rob, kill, revive, give, duel, gift)
- Hidden admin commands (only work for IDs in ADMIN_IDS, silently ignored for everyone else)
- Basic anti-spam / flood protection

Stack: python-telegram-bot v20.7, SQLite (matches your LunarNIXbot setup)
"""

import os
import sqlite3
import time
import random
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta

from telegram import Update
from telegram.constants import DiceEmoji
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("starkbot")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
# Comma-separated numeric IDs, e.g. "7140576750,111222333"
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "7140576750").split(",") if x.strip()}
DAILY_AMOUNT = 1000
GIFT_TAX = 0.10
ROB_PERCENT = 0.30
KILL_HOURS = 12
REVIVE_COST = 300
PROTECT_HOURS = 24

# Anti-spam settings
SPAM_WINDOW_SECONDS = 8
SPAM_MAX_MESSAGES = 5
SPAM_MUTE_SECONDS = 60

DB_PATH = "lunar_economy.db"

# ---------------------------------------------------------------------------
# DB SETUP
# ---------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        balance INTEGER DEFAULT 0,
        kills INTEGER DEFAULT 0,
        dead_until INTEGER DEFAULT 0,
        protected_until INTEGER DEFAULT 0,
        last_daily INTEGER DEFAULT 0,
        streak INTEGER DEFAULT 0,
        last_work INTEGER DEFAULT 0,
        title TEXT DEFAULT '',
        inventory TEXT DEFAULT '',
        banned INTEGER DEFAULT 0
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS marriages (
        user_id INTEGER PRIMARY KEY,
        partner_id INTEGER,
        partner_name TEXT,
        married_at INTEGER
    )""")
    conn.commit()
    conn.close()

def get_setting(key, default=None):
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )
    conn.commit()
    conn.close()

def get_user(user_id, username=None):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO users (user_id, username, balance) VALUES (?,?,?)",
            (user_id, username or "", 100)  # starting balance
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    elif username and row["username"] != username:
        conn.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
        conn.commit()
    conn.close()
    return row

def get_marriage(user_id):
    conn = db()
    row = conn.execute("SELECT * FROM marriages WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row

def set_marriage(user_id, partner_id, partner_name):
    conn = db()
    conn.execute(
        "INSERT INTO marriages (user_id, partner_id, partner_name, married_at) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET partner_id=excluded.partner_id, "
        "partner_name=excluded.partner_name, married_at=excluded.married_at",
        (user_id, partner_id, partner_name, int(time.time()))
    )
    conn.commit()
    conn.close()

def clear_marriage(user_id):
    conn = db()
    conn.execute("DELETE FROM marriages WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


    conn = db()
    conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))
    conn.commit()
    conn.close()

def set_field(user_id, field, value):
    conn = db()
    conn.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, user_id))
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# ANTI-SPAM
# ---------------------------------------------------------------------------
_msg_log = defaultdict(lambda: deque(maxlen=SPAM_MAX_MESSAGES))
_muted_until = {}
_lottery_pools = {}  # chat_id -> list of (user_id, name, amount)
_pending_proposals = {}  # target_user_id -> (proposer_id, proposer_name, expires_at)

async def antispam_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_setting("spam_protection", "on") != "on":
        return True

    user = update.effective_user
    if not user or user.id in ADMIN_IDS:
        return True
    now = time.time()

    window = int(get_setting("spam_window", SPAM_WINDOW_SECONDS))
    max_msgs = int(get_setting("spam_max_msgs", SPAM_MAX_MESSAGES))
    mute_secs = int(get_setting("spam_mute_secs", SPAM_MUTE_SECONDS))

    if user.id in _muted_until:
        if now < _muted_until[user.id]:
            try:
                await update.message.delete()
            except Exception:
                pass
            return False
        else:
            del _muted_until[user.id]

    dq = _msg_log[user.id]
    if dq.maxlen != max_msgs:
        dq = deque(dq, maxlen=max_msgs)
        _msg_log[user.id] = dq
    dq.append(now)
    if len(dq) == dq.maxlen and now - dq[0] < window:
        _muted_until[user.id] = now + mute_secs
        try:
            await context.bot.restrict_chat_member(
                update.effective_chat.id, user.id,
                permissions=None,  # set proper ChatPermissions(can_send_messages=False) in real use
                until_date=int(now + mute_secs)
            )
        except Exception as e:
            log.warning(f"Could not mute {user.id}: {e}")
        await update.message.reply_text(
            f"🚫 {user.first_name} spamming detected — muted for {mute_secs}s."
        )
        return False
    return True

async def spam_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run before every command — call manually at top of each handler, or
    attach as a MessageHandler group=0 that runs first (see main())."""
    return await antispam_filter(update, context)

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def is_dead(row):
    return row["dead_until"] and row["dead_until"] > int(time.time())

def is_protected(row):
    return row["protected_until"] and row["protected_until"] > int(time.time())

async def get_target(update: Update):
    """Reply-to-message based target, like /kill (reply)"""
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    return None

# ---------------------------------------------------------------------------
# BASIC ECONOMY COMMANDS
# ---------------------------------------------------------------------------
async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    row = get_user(u.id, u.username)
    now = int(time.time())
    if now - row["last_daily"] < 86400:
        remaining = 86400 - (now - row["last_daily"])
        h, m = remaining // 3600, (remaining % 3600) // 60
        await update.message.reply_text(f"⏳ Already claimed. Try again in {h}h {m}m.")
        return
    # streak resets if more than 48h since last claim, otherwise increments
    if now - row["last_daily"] <= 172800 and row["last_daily"] != 0:
        streak = row["streak"] + 1
    else:
        streak = 1
    bonus = min(streak * 100, 1000)  # +$100 per streak day, capped at +$1000
    total = DAILY_AMOUNT + bonus
    update_balance(u.id, total)
    set_field(u.id, "last_daily", now)
    set_field(u.id, "streak", streak)
    await update.message.reply_text(
        f"💰 +${total} daily coins claimed! (base ${DAILY_AMOUNT} + streak bonus ${bonus})\n"
        f"🔥 Streak: {streak} day{'s' if streak != 1 else ''}"
    )

async def cmd_bal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    target_user = (await get_target(update)) or update.effective_user
    row = get_user(target_user.id, target_user.username)
    conn = db()
    rank_row = conn.execute(
        "SELECT COUNT(*)+1 AS rank FROM users WHERE balance > ?", (row["balance"],)
    ).fetchone()
    conn.close()
    await update.message.reply_text(
        f"👤 {target_user.first_name}\n💰 Balance: ${row['balance']}\n🏆 Rank: #{rank_row['rank']}"
    )

async def cmd_give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    target = await get_target(update)
    if not target:
        await update.message.reply_text("↩️ Reply to the user you want to give coins to.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("ℹ️ Usage: /give (reply) <amount>")
        return
    amount = int(context.args[0])
    row = get_user(u.id, u.username)
    if amount <= 0 or row["balance"] < amount:
        await update.message.reply_text("❌ Insufficient balance.")
        return
    tax = int(amount * GIFT_TAX)
    net = amount - tax
    get_user(target.id, target.username)
    update_balance(u.id, -amount)
    update_balance(target.id, net)
    await update.message.reply_text(
        f"🎁 {u.first_name} gifted ${net} to {target.first_name} (tax: ${tax})"
    )

# ---------------------------------------------------------------------------
# GAMES — using Telegram's real emoji dice (send_dice) for provably-visible RNG
# ---------------------------------------------------------------------------
WORK_COOLDOWN = 1800  # 30 minutes
WORK_MIN, WORK_MAX = 50, 250
WORK_LINES = [
    "delivered pizzas across town 🍕",
    "fixed a broken VPS at 3am 🖥️",
    "walked someone's dog 🐕",
    "sold lemonade on the street 🍋",
    "did freelance coding gigs 💻",
    "busked with a guitar downtown 🎸",
]

SHOP_ITEMS = {
    "vip": {"price": 5000, "label": "👑 VIP Title"},
    "ninja": {"price": 3000, "label": "🥷 Ninja Title"},
    "lucky_charm": {"price": 2000, "label": "🍀 Lucky Charm (cosmetic)"},
    "crown": {"price": 8000, "label": "👑 Golden Crown (cosmetic)"},
}

async def cmd_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    row = get_user(u.id, u.username)
    now = int(time.time())
    if now - row["last_work"] < WORK_COOLDOWN:
        remaining = WORK_COOLDOWN - (now - row["last_work"])
        m, s = remaining // 60, remaining % 60
        await update.message.reply_text(f"⏳ Tired from last job. Rest {m}m {s}s more.")
        return
    earned = random.randint(WORK_MIN, WORK_MAX)
    update_balance(u.id, earned)
    set_field(u.id, "last_work", now)
    await update.message.reply_text(f"🛠️ You {random.choice(WORK_LINES)} and earned ${earned}!")

async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    lines = ["🛒 Lunar Shop:"]
    for key, item in SHOP_ITEMS.items():
        lines.append(f"• {item['label']} — ${item['price']}  (/buy {key})")
    await update.message.reply_text("\n".join(lines))

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    row = get_user(u.id, u.username)
    if not context.args or context.args[0].lower() not in SHOP_ITEMS:
        await update.message.reply_text("ℹ️ Usage: /buy <item_key>. See /shop for options.")
        return
    key = context.args[0].lower()
    item = SHOP_ITEMS[key]
    if row["balance"] < item["price"]:
        await update.message.reply_text("❌ Not enough coins.")
        return
    inv = set(row["inventory"].split(",")) if row["inventory"] else set()
    if key in inv:
        await update.message.reply_text("📦 You already own this.")
        return
    inv.add(key)
    update_balance(u.id, -item["price"])
    set_field(u.id, "inventory", ",".join(inv))
    await update.message.reply_text(f"✅ Purchased {item['label']}! Use /settitle {key} to equip a title item.")

async def cmd_settitle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    row = get_user(u.id, u.username)
    if not context.args:
        set_field(u.id, "title", "")
        await update.message.reply_text("🧹 Title cleared.")
        return
    key = context.args[0].lower()
    inv = set(row["inventory"].split(",")) if row["inventory"] else set()
    if key not in inv or key not in SHOP_ITEMS:
        await update.message.reply_text("🚫 You don't own that item.")
        return
    set_field(u.id, "title", SHOP_ITEMS[key]["label"])
    await update.message.reply_text(f"✅ Title equipped: {SHOP_ITEMS[key]['label']}")

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    target_user = (await get_target(update)) or update.effective_user
    row = get_user(target_user.id, target_user.username)
    m = get_marriage(target_user.id)
    inv = row["inventory"].split(",") if row["inventory"] else []
    inv_labels = [SHOP_ITEMS[k]["label"] for k in inv if k in SHOP_ITEMS]
    text = (
        f"👤 {target_user.first_name} {('— ' + row['title']) if row['title'] else ''}\n"
        f"💰 Balance: ${row['balance']}\n"
        f"💀 Kills: {row['kills']}\n"
        f"🔥 Daily streak: {row['streak']}\n"
        f"💞 Married to: {m['partner_name'] if m else 'Nobody'}\n"
        f"🎒 Inventory: {', '.join(inv_labels) if inv_labels else 'Empty'}"
    )
    await update.message.reply_text(text)


async def cmd_bet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /bet <amount> <1-6> — uses Telegram dice emoji """
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    row = get_user(u.id, u.username)
    if len(context.args) < 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        await update.message.reply_text("ℹ️ Usage: /bet <amount> <1-6>")
        return
    amount, guess = int(context.args[0]), int(context.args[1])
    if guess < 1 or guess > 6:
        await update.message.reply_text("🎲 Pick a number 1-6.")
        return
    if amount <= 0 or row["balance"] < amount:
        await update.message.reply_text("❌ Insufficient balance.")
        return
    update_balance(u.id, -amount)
    dice_msg = await update.message.reply_dice(emoji=DiceEmoji.DICE)
    result = dice_msg.dice.value
    if result == guess:
        win = amount * 5
        update_balance(u.id, win)
        await update.message.reply_text(f"🎲 Rolled {result}! You WON ${win} (5x)!")
    else:
        await update.message.reply_text(f"🎲 Rolled {result}. You lost ${amount}.")

async def cmd_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /slots <amount> — uses Telegram slot machine emoji """
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    row = get_user(u.id, u.username)
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("ℹ️ Usage: /slots <amount>")
        return
    amount = int(context.args[0])
    if amount <= 0 or row["balance"] < amount:
        await update.message.reply_text("❌ Insufficient balance.")
        return
    update_balance(u.id, -amount)
    dice_msg = await update.message.reply_dice(emoji=DiceEmoji.SLOT_MACHINE)
    value = dice_msg.dice.value  # 1-64, 64 = jackpot (777)
    if value == 64:
        win = amount * 10
        update_balance(u.id, win)
        await update.message.reply_text(f"🎰 JACKPOT 777! You won ${win} (10x)!")
    elif value in (1, 22, 43):  # any matching triple (bar/grape/lemon triples in TG's table)
        win = amount * 3
        update_balance(u.id, win)
        await update.message.reply_text(f"🎰 Triple match! You won ${win} (3x)!")
    else:
        await update.message.reply_text(f"🎰 No match. You lost ${amount}.")

async def cmd_flip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /flip <amount> <h/t> — uses Telegram's basketball emoji dice.
        Telegram's basketball returns 1-5: values 4-5 = "scored" (mapped to heads),
        values 1-3 = "missed" (mapped to tails). Note this isn't a perfect 50/50
        split (scoring is slightly less likely than missing), but it's driven
        entirely by Telegram's own animation/result, same as the other games. """
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    row = get_user(u.id, u.username)
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("ℹ️ Usage: /flip <amount> <h/t>")
        return
    amount = int(context.args[0])
    pick = context.args[1].lower()
    if pick not in ("h", "t", "heads", "tails"):
        await update.message.reply_text("🪙 Pick must be h or t.")
        return
    pick = "heads" if pick.startswith("h") else "tails"
    if amount <= 0 or row["balance"] < amount:
        await update.message.reply_text("❌ Insufficient balance.")
        return
    update_balance(u.id, -amount)
    dice_msg = await update.message.reply_dice(emoji=DiceEmoji.BASKETBALL)
    # Basketball value: 4-5 = scored -> heads, 1-3 = missed -> tails
    result = "heads" if dice_msg.dice.value >= 4 else "tails"
    if result == pick:
        win = amount * 2
        update_balance(u.id, win)
        await update.message.reply_text(f"🏀 {'Scored' if result=='heads' else 'Missed'} ({result.upper()}) — You WON ${win} (2x)!")
    else:
        await update.message.reply_text(f"🏀 {'Scored' if result=='heads' else 'Missed'} ({result.upper()}) — You lost ${amount}.")

async def cmd_roulette(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /roulette <amount> — uses Telegram dart emoji, bullseye = 35x """
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    row = get_user(u.id, u.username)
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("ℹ️ Usage: /roulette <amount>")
        return
    amount = int(context.args[0])
    if amount <= 0 or row["balance"] < amount:
        await update.message.reply_text("❌ Insufficient balance.")
        return
    update_balance(u.id, -amount)
    dice_msg = await update.message.reply_dice(emoji=DiceEmoji.DARTS)
    value = dice_msg.dice.value  # 6 = bullseye
    if value == 6:
        win = amount * 35
        update_balance(u.id, win)
        await update.message.reply_text(f"🎯 BULLSEYE! You won ${win} (35x)!")
    else:
        await update.message.reply_text(f"🎯 Missed center. You lost ${amount}.")

async def cmd_color(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /c <amount> — uses Telegram bowling emoji mapped to 4 colors """
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    row = get_user(u.id, u.username)
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("ℹ️ Usage: /c <amount> then pick a color when prompted: red/green/blue/gold")
        return
    amount = int(context.args[0])
    colors = ["🔴 red", "🟢 green", "🔵 blue", "🟡 gold"]
    if len(context.args) >= 2 and context.args[1].lower() in ("red", "green", "blue", "gold"):
        pick = context.args[1].lower()
    else:
        await update.message.reply_text("ℹ️ Usage: /c <amount> <red|green|blue|gold>")
        return
    if amount <= 0 or row["balance"] < amount:
        await update.message.reply_text("❌ Insufficient balance.")
        return
    update_balance(u.id, -amount)
    dice_msg = await update.message.reply_dice(emoji=DiceEmoji.BOWLING)
    value = dice_msg.dice.value  # 1-6
    mapped = ["red", "green", "blue", "gold"][value % 4]
    if mapped == pick:
        win = amount * 4
        update_balance(u.id, win)
        await update.message.reply_text(f"🎨 Landed on {mapped.upper()}! You won ${win} (4x)!")
    else:
        await update.message.reply_text(f"🎨 Landed on {mapped.upper()}. You lost ${amount}.")

# ---------------------------------------------------------------------------
# PLAYER INTERACTION COMMANDS
# ---------------------------------------------------------------------------
async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    target = await get_target(update)
    if not target:
        await update.message.reply_text("↩️ Reply to the user you want to kill.")
        return
    trow = get_user(target.id, target.username)
    if is_protected(trow):
        await update.message.reply_text(f"🛡️ {target.first_name} is protected!")
        return
    until = int(time.time()) + KILL_HOURS * 3600
    set_field(target.id, "dead_until", until)
    set_field(u.id, "kills", get_user(u.id)["kills"] + 1)
    await update.message.reply_text(f"💀 {target.first_name} has been killed for {KILL_HOURS}h!")

async def cmd_revive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    target = await get_target(update)
    if not target:
        await update.message.reply_text("↩️ Reply to the dead user you want to revive.")
        return
    row = get_user(u.id, u.username)
    if row["balance"] < REVIVE_COST:
        await update.message.reply_text(f"❌ Revive costs ${REVIVE_COST}.")
        return
    update_balance(u.id, -REVIVE_COST)
    set_field(target.id, "dead_until", 0)
    await update.message.reply_text(f"❤️ {target.first_name} has been revived by {u.first_name}!")

async def cmd_rob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    target = await get_target(update)
    if not target:
        await update.message.reply_text("↩️ Reply to the user you want to rob.")
        return
    trow = get_user(target.id, target.username)
    if is_protected(trow):
        await update.message.reply_text(f"🛡️ {target.first_name} is protected!")
        return
    if trow["balance"] <= 0:
        await update.message.reply_text("🤷 They have nothing to steal.")
        return
    stolen = int(trow["balance"] * ROB_PERCENT)
    update_balance(target.id, -stolen)
    update_balance(u.id, stolen)
    await update.message.reply_text(f"🥷 {u.first_name} robbed ${stolen} from {target.first_name}!")

async def cmd_protect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    until = int(time.time()) + PROTECT_HOURS * 3600
    set_field(u.id, "protected_until", until)
    await update.message.reply_text(f"🛡️ You are protected for {PROTECT_HOURS}h!")

async def cmd_duel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /duel <amount> (reply) — both wager, dice decides winner """
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    target = await get_target(update)
    if not target:
        await update.message.reply_text("↩️ Reply to the user you want to duel.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("ℹ️ Usage: /duel <amount> (reply to opponent)")
        return
    amount = int(context.args[0])
    urow, trow = get_user(u.id, u.username), get_user(target.id, target.username)
    if urow["balance"] < amount or trow["balance"] < amount:
        await update.message.reply_text("❌ One of you doesn't have enough coins.")
        return
    dice1 = await update.message.reply_dice(emoji=DiceEmoji.DICE)
    dice2 = await update.message.reply_dice(emoji=DiceEmoji.DICE)
    v1, v2 = dice1.dice.value, dice2.dice.value
    if v1 == v2:
        await update.message.reply_text(f"🎲 Tie ({v1} - {v2})! No coins exchanged.")
        return
    if v1 > v2:
        winner, loser = u, target
    else:
        winner, loser = target, u
    update_balance(winner.id, amount)
    update_balance(loser.id, -amount)
    await update.message.reply_text(
        f"⚔️ {u.first_name} ({v1}) vs {target.first_name} ({v2})\n🏆 {winner.first_name} wins ${amount}!"
    )

# ---------------------------------------------------------------------------
# LEADERBOARD
# ---------------------------------------------------------------------------
async def cmd_rps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /rps <amount> <rock|paper|scissors> (reply to opponent) — both must run it within 30s,
        simplified here as: challenger picks, opponent's choice is randomized for speed,
        winner takes the pot. """
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    target = await get_target(update)
    if not target:
        await update.message.reply_text("↩️ Reply to the user you want to challenge.")
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("ℹ️ Usage: /rps <amount> <rock|paper|scissors> (reply to opponent)")
        return
    amount = int(context.args[0])
    pick = context.args[1].lower()
    choices = ("rock", "paper", "scissors")
    if pick not in choices:
        await update.message.reply_text("✊✋✌️ Choose rock, paper, or scissors.")
        return
    urow, trow = get_user(u.id, u.username), get_user(target.id, target.username)
    if urow["balance"] < amount or trow["balance"] < amount:
        await update.message.reply_text("❌ One of you doesn't have enough coins.")
        return
    opp_pick = random.choice(choices)
    beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    if pick == opp_pick:
        result = f"🤝 Both picked {pick}! Tie — no coins exchanged."
    elif beats[pick] == opp_pick:
        update_balance(u.id, amount)
        update_balance(target.id, -amount)
        result = f"✊✋✌️ {u.first_name} ({pick}) beats {target.first_name} ({opp_pick})! +${amount}"
    else:
        update_balance(target.id, amount)
        update_balance(u.id, -amount)
        result = f"✊✋✌️ {target.first_name} ({opp_pick}) beats {u.first_name} ({pick})! +${amount} for {target.first_name}"
    await update.message.reply_text(result)

async def cmd_slap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ cosmetic-only player interaction, no coins involved """
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    target = await get_target(update)
    if not target:
        await update.message.reply_text("↩️ Reply to the user you want to slap.")
        return
    slaps = ["👋💥", "🖐️💢", "✋😵"]
    await update.message.reply_text(
        f"{u.first_name} slaps {target.first_name}! {random.choice(slaps)}"
    )

async def cmd_hug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ cosmetic-only player interaction, no coins involved """
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    target = await get_target(update)
    if not target:
        await update.message.reply_text("↩️ Reply to the user you want to hug.")
        return
    await update.message.reply_text(f"🤗 {u.first_name} hugs {target.first_name} warmly!")


    if not await spam_guard(update, context):
        return
    conn = db()
    rows = conn.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10").fetchall()
    conn.close()
    text = "💰 Top 10 Richest:\n" + "\n".join(
        f"{i+1}. {r['username'] or 'Unknown'} — ${r['balance']}" for i, r in enumerate(rows)
    )
    await update.message.reply_text(text)

async def cmd_lottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /lottery <amount> — join the pool; run /drawlottery once 2+ players joined """
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    row = get_user(u.id, u.username)
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("ℹ️ Usage: /lottery <amount>")
        return
    amount = int(context.args[0])
    if amount <= 0 or row["balance"] < amount:
        await update.message.reply_text("❌ Insufficient balance.")
        return
    chat_id = update.effective_chat.id
    pool = _lottery_pools.setdefault(chat_id, [])
    if any(p[0] == u.id for p in pool):
        await update.message.reply_text("🎟️ You already joined this round.")
        return
    update_balance(u.id, -amount)
    pool.append((u.id, u.first_name, amount))
    await update.message.reply_text(
        f"🎟️ {u.first_name} joined the lottery with ${amount}! "
        f"Pool: {len(pool)} player(s), total ${sum(p[2] for p in pool)}.\n"
        f"Run /drawlottery anytime with 2+ players to draw a winner."
    )

async def cmd_drawlottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    chat_id = update.effective_chat.id
    pool = _lottery_pools.get(chat_id, [])
    if len(pool) < 2:
        await update.message.reply_text("⏳ Need at least 2 players in the pool. Use /lottery <amount> to join.")
        return
    total = sum(p[2] for p in pool)
    winner_id, winner_name, _ = random.choice(pool)
    update_balance(winner_id, total)
    _lottery_pools[chat_id] = []
    names = ", ".join(p[1] for p in pool)
    await update.message.reply_text(
        f"🎉 Lottery draw! Players: {names}\n🏆 Winner: {winner_name} takes the pot of ${total}!"
    )

async def cmd_scratch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /scratch <amount> — instant scratch card, match all 3 symbols to win """
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    row = get_user(u.id, u.username)
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("ℹ️ Usage: /scratch <amount>")
        return
    amount = int(context.args[0])
    if amount <= 0 or row["balance"] < amount:
        await update.message.reply_text("❌ Insufficient balance.")
        return
    update_balance(u.id, -amount)
    symbols = ["🍒", "🍋", "⭐", "💎", "🔔"]
    weights = [40, 30, 15, 10, 5]
    card = random.choices(symbols, weights=weights, k=3)
    card_text = " | ".join(card)
    if card[0] == card[1] == card[2]:
        payout_table = {"🍒": 3, "🍋": 4, "⭐": 6, "💎": 15, "🔔": 25}
        win = amount * payout_table[card[0]]
        update_balance(u.id, win)
        await update.message.reply_text(f"🎫 [ {card_text} ] — JACKPOT MATCH! You won ${win}!")
    else:
        await update.message.reply_text(f"🎫 [ {card_text} ] — No match. You lost ${amount}.")

async def cmd_propose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /propose (reply) — sends a marriage proposal that target must /accept within 60s """
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    target = await get_target(update)
    if not target:
        await update.message.reply_text("↩️ Reply to the user you want to propose to.")
        return
    if target.id == u.id:
        await update.message.reply_text("You can't marry yourself 😂")
        return
    if get_marriage(u.id):
        await update.message.reply_text("💍 You're already married! Use /divorce first.")
        return
    if get_marriage(target.id):
        await update.message.reply_text(f"{target.first_name} is already married.")
        return
    _pending_proposals[target.id] = (u.id, u.first_name, time.time() + 60)
    lines = [
        f"💍 {u.first_name} got down on one knee and proposed to {target.first_name}!",
        f"💐 {u.first_name} is proposing to {target.first_name}... will they say yes?",
        f"💖 {u.first_name} popped the question to {target.first_name}!",
    ]
    await update.message.reply_text(
        f"{random.choice(lines)}\n{target.first_name}, reply with /accept within 60s to say yes!"
    )

async def cmd_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    pending = _pending_proposals.get(u.id)
    if not pending or time.time() > pending[2]:
        await update.message.reply_text("💔 No active proposal for you (or it expired).")
        _pending_proposals.pop(u.id, None)
        return
    proposer_id, proposer_name, _ = pending
    get_user(proposer_id)
    get_user(u.id, u.username)
    set_marriage(proposer_id, u.id, u.first_name)
    set_marriage(u.id, proposer_id, proposer_name)
    del _pending_proposals[u.id]
    await update.message.reply_text(
        f"💒 Congratulations! {proposer_name} and {u.first_name} are now married! 🎉👰🤵"
    )

async def cmd_divorce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    m = get_marriage(u.id)
    if not m:
        await update.message.reply_text("💔 You're not married.")
        return
    clear_marriage(u.id)
    clear_marriage(m["partner_id"])
    await update.message.reply_text(f"💔 {u.first_name} and {m['partner_name']} are now divorced.")

async def cmd_couple(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    u = update.effective_user
    m = get_marriage(u.id)
    if not m:
        await update.message.reply_text("💔 You're not married. Use /propose (reply) to start a relationship!")
        return
    days = int((time.time() - m["married_at"]) // 86400)
    await update.message.reply_text(
        f"💞 {u.first_name} is married to {m['partner_name']} ({days} day{'s' if days != 1 else ''} together)"
    )

def _cosmetic_command(verb, emoji_lines):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await spam_guard(update, context):
            return
        u = update.effective_user
        target = await get_target(update)
        if not target:
            await update.message.reply_text(f"↩️ Reply to the user you want to {verb}.")
            return
        line = random.choice(emoji_lines).format(u=u.first_name, t=target.first_name)
        await update.message.reply_text(line)
    return handler

cmd_kiss = _cosmetic_command("kiss", [
    "{u} kisses {t} 😘💋",
    "{u} leans in and kisses {t} softly 💞",
    "{u} plants a surprise kiss on {t}! 😳💋",
])
cmd_pat = _cosmetic_command("pat", [
    "{u} pats {t} on the head 🥰✋",
    "{u} gives {t} a gentle headpat 🤍",
])
cmd_poke = _cosmetic_command("poke", [
    "{u} pokes {t} 👉😆",
    "{u} keeps poking {t} repeatedly 👉👉👉",
])
cmd_highfive = _cosmetic_command("high-five", [
    "{u} high-fives {t}! 🙌🔥",
    "{u} and {t} slap a high five! ✋✋",
])
cmd_cuddle = _cosmetic_command("cuddle", [
    "{u} cuddles up with {t} 🥰🧸",
    "{u} wraps {t} in a cozy cuddle 🤗💤",
])
cmd_dance = _cosmetic_command("dance with", [
    "{u} grabs {t}'s hand and they dance together! 💃🕺",
    "{u} and {t} bust out some moves on the dance floor 🕺💃🎶",
])


async def cmd_toprich(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    conn = db()
    rows = conn.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10").fetchall()
    conn.close()
    text = "💰 Top 10 Richest:\n" + "\n".join(
        f"{i+1}. {r['username'] or 'Unknown'} — ${r['balance']}" for i, r in enumerate(rows)
    )
    await update.message.reply_text(text)

async def cmd_topkill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    conn = db()
    rows = conn.execute("SELECT username, kills FROM users ORDER BY kills DESC LIMIT 10").fetchall()
    conn.close()
    text = "💀 Top 10 Killers:\n" + "\n".join(
        f"{i+1}. {r['username'] or 'Unknown'} — {r['kills']} kills" for i, r in enumerate(rows)
    )
    await update.message.reply_text(text)

# ---------------------------------------------------------------------------
# HIDDEN ADMIN COMMANDS
# These are real commands, but silently do nothing for non-admins so
# no one even learns they exist (no "you are not authorized" reply, which
# would give the game away). They also won't show in /help below.
# ---------------------------------------------------------------------------
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            return  # silently ignore, no trace in chat
        return await func(update, context)
    return wrapper

@admin_only
async def cmd_addcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await get_target(update)
    if not target or not context.args or not context.args[0].lstrip("-").isdigit():
        return
    amount = int(context.args[0])
    get_user(target.id, target.username)
    update_balance(target.id, amount)
    await update.message.reply_text(f"✅ Adjusted {target.first_name}'s balance by {amount}.")

@admin_only
async def cmd_setbal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await get_target(update)
    if not target or not context.args or not context.args[0].isdigit():
        return
    set_field(target.id, "balance", int(context.args[0]))
    await update.message.reply_text(f"✅ Set {target.first_name}'s balance to {context.args[0]}.")

@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await get_target(update)
    if not target:
        return
    set_field(target.id, "banned", 1)
    await update.message.reply_text(f"🚫 {target.first_name} banned from economy.")

@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await get_target(update)
    if not target:
        return
    set_field(target.id, "banned", 0)
    await update.message.reply_text(f"✅ {target.first_name} unbanned.")

@admin_only
async def cmd_resetuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await get_target(update)
    if not target:
        return
    conn = db()
    conn.execute("DELETE FROM users WHERE user_id=?", (target.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"♻️ Reset {target.first_name}'s data.")

@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return
    text = " ".join(context.args)
    conn = db()
    ids = [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]
    conn.close()
    sent = 0
    for uid in ids:
        try:
            await context.bot.send_message(uid, f"📢 {text}")
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"✅ Broadcast sent to {sent} users.")

@admin_only
async def cmd_spamtoggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("ℹ️ Usage: /spamtoggle on|off")
        return
    set_setting("spam_protection", context.args[0].lower())
    await update.message.reply_text(f"✅ Spam protection turned {context.args[0].upper()}.")

@admin_only
async def cmd_spamset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /spamset <window_secs> <max_msgs> <mute_secs> """
    if len(context.args) < 3 or not all(a.isdigit() for a in context.args[:3]):
        await update.message.reply_text(
            "ℹ️ Usage: /spamset <window_seconds> <max_messages> <mute_seconds>"
        )
        return
    window, max_msgs, mute_secs = (int(x) for x in context.args[:3])
    set_setting("spam_window", window)
    set_setting("spam_max_msgs", max_msgs)
    set_setting("spam_mute_secs", mute_secs)
    await update.message.reply_text(
        f"✅ Spam settings updated: {max_msgs} msgs / {window}s window → mute {mute_secs}s"
    )

@admin_only
async def cmd_spamstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = get_setting("spam_protection", "on")
    window = get_setting("spam_window", SPAM_WINDOW_SECONDS)
    max_msgs = get_setting("spam_max_msgs", SPAM_MAX_MESSAGES)
    mute_secs = get_setting("spam_mute_secs", SPAM_MUTE_SECONDS)
    await update.message.reply_text(
        f"🛡️ Spam protection: {status.upper()}\n"
        f"Window: {window}s | Max msgs: {max_msgs} | Mute: {mute_secs}s"
    )


    if not context.args:
        return
    text = " ".join(context.args)
    conn = db()
    ids = [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]
    conn.close()
    sent = 0
    for uid in ids:
        try:
            await context.bot.send_message(uid, f"📢 {text}")
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"✅ Broadcast sent to {sent} users.")

# ---------------------------------------------------------------------------
# HELP (admin commands intentionally excluded)
# ---------------------------------------------------------------------------
async def cmd_start_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await spam_guard(update, context):
        return
    text = (
        "🌙 Welcome to Lunar Economy 🔥!\n"
        "Yaha coins kamao, loot maro, kill karo aur jeeto!\n\n"
        "⭐ Coin Commands:\n"
        "/daily — daily $1000 coins (+streak bonus)\n"
        "/bal — balance + rank\n"
        "/give (reply) <amount> — gift coins (10% tax)\n"
        "/work — earn coins (30 min cooldown)\n"
        "/shop — browse cosmetic items\n"
        "/buy <item> — purchase item\n"
        "/settitle <item> — equip a purchased title\n"
        "/profile (reply optional) — view stats card\n\n"
        "🎮 Action Commands:\n"
        "/kill (reply) — kill target for 12h\n"
        "/rob (reply) — steal 30% of target's coins\n"
        "/protect — 24h protection\n"
        "/revive (reply) — revive a dead user ($300)\n"
        "/duel <amount> (reply) — wager duel vs another player\n"
        "/rps <amount> <rock|paper|scissors> (reply) — rock-paper-scissors wager\n"
        "/slap (reply) — slap someone (just for fun)\n"
        "/hug (reply) — hug someone (just for fun)\n\n"
        "💕 Cosmetic / Relationship:\n"
        "/kiss /pat /poke /highfive /cuddle /dance (reply) — fun interactions\n"
        "/propose (reply) — propose marriage\n"
        "/accept — accept a pending proposal\n"
        "/divorce — end your marriage\n"
        "/couple — check your relationship status\n\n"
        "🎲 Gaming:\n"
        "/bet <amount> <1-6> — dice (5x)\n"
        "/slots <amount> — slot machine (10x jackpot)\n"
        "/flip <amount> <h/t> — basketball flip (2x)\n"
        "/roulette <amount> — dart roulette (35x)\n"
        "/c <amount> <color> — color prediction (4x)\n"
        "/lottery <amount> — join lottery pool\n"
        "/drawlottery — draw winner (2+ players)\n"
        "/scratch <amount> — scratch card game\n\n"
        "🏆 Leaderboard:\n"
        "/toprich — top 10 richest\n"
        "/topkill — top 10 killers"
    )
    await update.message.reply_text(text)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
async def error_handler(update, context):
    log.error("Exception while handling update:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong running that command. The error has been logged."
            )
    except Exception:
        pass

def main():
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise SystemExit(
            "ERROR: BOT_TOKEN not set. Set the BOT_TOKEN environment variable "
            "(see LunarEconomy.service) or edit bot.py directly."
        )
    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .get_updates_read_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler(["start", "help"], cmd_start_help))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("bal", cmd_bal))
    app.add_handler(CommandHandler("give", cmd_give))
    app.add_handler(CommandHandler("work", cmd_work))
    app.add_handler(CommandHandler("shop", cmd_shop))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("settitle", cmd_settitle))
    app.add_handler(CommandHandler("profile", cmd_profile))

    app.add_handler(CommandHandler("bet", cmd_bet))
    app.add_handler(CommandHandler("slots", cmd_slots))
    app.add_handler(CommandHandler("flip", cmd_flip))
    app.add_handler(CommandHandler("roulette", cmd_roulette))
    app.add_handler(CommandHandler("c", cmd_color))
    app.add_handler(CommandHandler("lottery", cmd_lottery))
    app.add_handler(CommandHandler("drawlottery", cmd_drawlottery))
    app.add_handler(CommandHandler("scratch", cmd_scratch))

    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("revive", cmd_revive))
    app.add_handler(CommandHandler("rob", cmd_rob))
    app.add_handler(CommandHandler("protect", cmd_protect))
    app.add_handler(CommandHandler("duel", cmd_duel))
    app.add_handler(CommandHandler("rps", cmd_rps))
    app.add_handler(CommandHandler("slap", cmd_slap))
    app.add_handler(CommandHandler("hug", cmd_hug))
    app.add_handler(CommandHandler("kiss", cmd_kiss))
    app.add_handler(CommandHandler("pat", cmd_pat))
    app.add_handler(CommandHandler("poke", cmd_poke))
    app.add_handler(CommandHandler("highfive", cmd_highfive))
    app.add_handler(CommandHandler("cuddle", cmd_cuddle))
    app.add_handler(CommandHandler("dance", cmd_dance))
    app.add_handler(CommandHandler("propose", cmd_propose))
    app.add_handler(CommandHandler("accept", cmd_accept))
    app.add_handler(CommandHandler("divorce", cmd_divorce))
    app.add_handler(CommandHandler("couple", cmd_couple))

    app.add_handler(CommandHandler("toprich", cmd_toprich))
    app.add_handler(CommandHandler("topkill", cmd_topkill))

    # Hidden admin commands — not listed in /help, silently no-op for non-admins
    app.add_handler(CommandHandler("addcoins", cmd_addcoins))
    app.add_handler(CommandHandler("setbal", cmd_setbal))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("resetuser", cmd_resetuser))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("spamtoggle", cmd_spamtoggle))
    app.add_handler(CommandHandler("spamset", cmd_spamset))
    app.add_handler(CommandHandler("spamstatus", cmd_spamstatus))

    app.add_error_handler(error_handler)

    log.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
