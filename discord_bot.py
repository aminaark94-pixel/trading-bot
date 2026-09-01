import os
import json
import base64
import asyncio
import requests
import discord
from discord.ext import tasks, commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
GH_PAT = os.getenv("GH_PAT")
GH_REPO = os.getenv("GH_REPO")  # Format: "username/repository"

GITHUB_API_BASE = f"https://api.github.com/repos/{GH_REPO}"
HEADERS = {
    "Authorization": f"Bearer {GH_PAT}",
    "Accept": "application/vnd.github+json"
}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="/", intents=intents)
posted_trade_ids = set()


# --- GitHub File API Helpers ---
def fetch_gh_file(path: str):
    url = f"{GITHUB_API_BASE}/contents/{path}"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        content = base64.b64decode(data['content']).decode('utf-8')
        return content, data['sha']
    return None, None


def update_gh_file(path: str, new_content: str, message: str):
    url = f"{GITHUB_API_BASE}/contents/{path}"
    _, sha = fetch_gh_file(path)
    if not sha:
        return False
    payload = {
        "message": message,
        "content": base64.b64encode(new_content.encode('utf-8')).decode('utf-8'),
        "sha": sha
    }
    resp = requests.put(url, headers=HEADERS, json=payload, timeout=10)
    return resp.status_code in [200, 201]


# --- 30-Second Signal Poller ---
@tasks.loop(seconds=30)
def poll_open_signals():
    global posted_trade_ids
    if not DISCORD_CHANNEL_ID or not GH_REPO or not GH_PAT:
        return

    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        return

    raw_content, _ = fetch_gh_file("signals/open.json")
    if not raw_content:
        return

    try:
        signals = json.loads(raw_content)
    except Exception:
        return

    for trade in signals:
        trade_id = trade.get("id")
        if trade_id and trade_id not in posted_trade_ids:
            posted_trade_ids.add(trade_id)
            side = trade.get("side", "BUY")
            color = discord.Color.green() if side == "BUY" else discord.Color.red()

            embed = discord.Embed(
                title=f"🚨 New Trade Signal: {trade.get('bot_name')} ({side})",
                color=color
            )
            embed.add_field(name="Symbol", value=trade.get("symbol"), inline=True)
            embed.add_field(name="Entry Price", value=f"${trade.get('entry_price'):,}", inline=True)
            embed.add_field(name="Position Size", value=f"${trade.get('position_size')} USDT", inline=True)
            embed.add_field(name="Take Profit", value=f"${trade.get('take_profit'):,}", inline=True)
            embed.add_field(name="Stop Loss", value=f"${trade.get('stop_loss'):,}", inline=True)
            embed.add_field(name="Trigger Time", value=trade.get("entry_time"), inline=False)
            embed.set_footer(text=f"Trade ID: {trade_id} • 24/7 Free Bot Matrix")

            asyncio.run_coroutine_threadsafe(channel.send(embed=embed), bot.loop)


@bot.event
async def on_ready():
    print(f"[DISCORD] Connected as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"[DISCORD] Registered {len(synced)} slash commands.")
    except Exception as e:
        print(f"[DISCORD] Failed registering slash commands: {e}")
    if not poll_open_signals.is_running():
        poll_open_signals.start()


# --- Slash Commands ---
@bot.tree.command(name="start", description="Enable 24/7 autonomous bot trading engine")
async def start_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    success = update_gh_file("config/bot_enabled.txt", "1", "chore: Enable bot execution via Discord")
    if success:
        await interaction.followup.send("✅ **Trading Engine Enabled!** Bots will run on the next 5-minute cycle.")
    else:
        await interaction.followup.send("❌ **Error:** Failed writing to GitHub repository.")


@bot.tree.command(name="stop", description="Disable 24/7 bot trading engine")
async def stop_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    success = update_gh_file("config/bot_enabled.txt", "0", "chore: Disable bot execution via Discord")
    if success:
        await interaction.followup.send("🛑 **Trading Engine Disabled!** All strategy evaluation halted.")
    else:
        await interaction.followup.send("❌ **Error:** Failed writing to GitHub repository.")


@bot.tree.command(name="status", description="Check current master switch status and active positions")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    enabled_txt, _ = fetch_gh_file("config/bot_enabled.txt")
    is_active = (enabled_txt.strip() == "1") if enabled_txt else False

    raw_open, _ = fetch_gh_file("signals/open.json")
    open_count = len(json.loads(raw_open)) if raw_open else 0

    embed = discord.Embed(
        title="🤖 Trading Matrix Status",
        color=discord.Color.green() if is_active else discord.Color.dark_grey()
    )
    embed.add_field(name="Engine State", value="🟢 ACTIVE (1)" if is_active else "🔴 PAUSED (0)", inline=True)
    embed.add_field(name="Open Signals", value=f"{open_count} trades", inline=True)
    embed.set_footer(text=f"Repo: {GH_REPO}")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="stats", description="View performance metrics, win rate, and total PnL")
async def stats_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    raw_stats, _ = fetch_gh_file("signals/stats.json")
    if not raw_stats:
        await interaction.followup.send("⚠️ Stats file not found.")
        return

    stats = json.loads(raw_stats)
    pnl = stats.get("total_pnl_usdt", 0.0)
    pnl_color = discord.Color.green() if pnl >= 0 else discord.Color.red()

    embed = discord.Embed(title="📊 Bot Performance Summary", color=pnl_color)
    embed.add_field(name="Total Net PnL", value=f"${pnl:+.2f} USDT", inline=True)
    embed.add_field(name="Win Rate", value=f"{stats.get('win_rate', 0.0)}%", inline=True)
    embed.add_field(name="Trades (W / L)", value=f"{stats.get('total_trades', 0)} ({stats.get('winning_trades', 0)}W / {stats.get('losing_trades', 0)}L)", inline=True)

    breakdown = ""
    for name, data in stats.get("bots", {}).items():
        breakdown += f"• **{name}**: {data.get('trades', 0)} trades | PnL: `${data.get('pnl', 0.0):+.2f}`\n"

    if breakdown:
        embed.add_field(name="Per-Bot Breakdown", value=breakdown, inline=False)
    embed.set_footer(text=f"Last updated: {stats.get('last_run', 'N/A')}")
    await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("[ERROR] DISCORD_BOT_TOKEN missing in environment.")
    else:
        bot.run(DISCORD_BOT_TOKEN)