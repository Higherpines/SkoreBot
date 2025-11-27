import os
import discord
from discord.ext import tasks
from discord import app_commands
import aiohttp
import asyncio
import json
from datetime import datetime, timezone, timedelta

# Load config
with open("config.json") as f:
    cfg = json.load(f)

CHANNEL_ID = cfg["channel_id"]
SCHOOL = cfg.get("school_name", "South Carolina")
SPORTS = cfg["sports"]
CHECK_INTERVAL = cfg.get("check_interval_seconds", 60)
PRE_GAME_MINUTES = cfg.get("pre_game_minutes", 30)

# Discord setup
intents = discord.Intents.default()
intents.message_content = False
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# State
last_updates = {}
last_status = {}
pre_notified = set()

# -----------------------------
# Utility Functions
# -----------------------------

async def fetch_json(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

def build_embed_for_play(sport_name, play, summary=None):
    emb = discord.Embed(title=f"{sport_name} — Scoring Play", color=0x91268f)
    emb.add_field(name="Play", value=play.get("text", "N/A"), inline=False)
    team = play.get("team", {}).get("displayName", "Team")
    emb.add_field(name="Team", value=team, inline=True)
    emb.add_field(name="Score", value=f"{play.get('awayScore','0')} - {play.get('homeScore','0')}", inline=True)
    if summary:
        emb.set_footer(text=summary)
    emb.timestamp = datetime.now(timezone.utc)
    return emb

# -----------------------------
# Main Game Checking Logic
# -----------------------------

async def check_sport(scoreboard_url, sport_name, channel):
    try:
        data = await fetch_json(scoreboard_url)
    except Exception as e:
        print(f"Failed to fetch {sport_name} scoreboard: {e}")
        return

    for event in data.get("events", []):
        game_id = event.get("id")
        comp = event.get("competitions", [None])[0]
        if comp is None:
            continue

        # Check if our school is playing
        competitors = comp.get("competitors", [])
        if not any(SCHOOL.lower() in c.get("team", {}).get("displayName", "").lower() for c in competitors):
            continue

        # Fetch summary
        summary_url = scoreboard_url.replace("scoreboard", f"summary?event={game_id}")
        try:
            summary = await fetch_json(summary_url)
        except Exception as e:
            print(f"Failed to fetch summary for {game_id}: {e}")
            continue

        # Scoring plays
        scoring = summary.get("scoringPlays", []) or []
        old = last_updates.get(game_id, [])
        if scoring != old:
            new = scoring[len(old):] if len(scoring) >= len(old) else scoring
            for play in new:
                emb = build_embed_for_play(sport_name, play)
                await channel.send(embed=emb)
            last_updates[game_id] = scoring

        # Status transitions
        status_type = comp.get("status", {}).get("type", {}).get("name", "").lower()
        prev = last_status.get(game_id)

        # Pre-game notifications
        if status_type in ("pre", "scheduled") or comp.get("status", {}).get("type", {}).get("description", "").lower().startswith("scheduled"):
            start_iso = event.get("date")
            if start_iso:
                start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                delta = start_dt - now
                if 0 < delta.total_seconds() <= PRE_GAME_MINUTES * 60:
                    if game_id not in pre_notified:
                        emb = discord.Embed(
                            title=f"Upcoming: {sport_name}",
                            description=f"{SCHOOL} plays in {int(delta.total_seconds()//60)} minutes."
                        )
                        emb.add_field(name="Starts", value=start_dt.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'))
                        await channel.send(embed=emb)
                        pre_notified.add(game_id)

        # Final summary
        if status_type in ("post", "completed", "final"):
            if prev not in ("post", "completed", "final"):
                emb = discord.Embed(title=f"{sport_name} — Final", description=f"Final score for {SCHOOL}")
                try:
                    comps = summary.get("competitions", [])[0].get("competitors", [])
                    for c in comps:
                        emb.add_field(name=c.get("team", {}).get("displayName", ""), value=c.get("score", "0"))
                except Exception:
                    pass
                await channel.send(embed=emb)

        last_status[game_id] = status_type

# -----------------------------
# Background Loop
# -----------------------------

@tasks.loop(seconds=CHECK_INTERVAL)
async def watcher_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("Channel not found — check CHANNEL_ID.")
        return

    for sport in SPORTS:
        await check_sport(sport["url"], sport["name"], channel)

# -----------------------------
# Slash Commands
# -----------------------------

@tree.command(name="score", description="Get current live score for USC.")
@app_commands.describe(sport_name="Optional sport name")
async def slash_score(interaction: discord.Interaction, sport_name: str = None):
    await interaction.response.defer()
    sport = next((s for s in SPORTS if s["name"].lower() == sport_name.lower()), SPORTS[0] if sport_name is None else None)
    if not sport:
        await interaction.followup.send("Sport not found.")
        return

    try:
        data = await fetch_json(sport["url"])
    except Exception as e:
        await interaction.followup.send(f"Failed to fetch scoreboard: {e}")
        return

    lines = []
    for event in data.get("events", []):
        comp = event.get("competitions", [None])[0]
        if not comp:
            continue
        if any(SCHOOL.lower() in c.get("team", {}).get("displayName", "").lower() for c in comp.get("competitors", [])):
            away = comp.get("competitors", [])[0]
            home = comp.get("competitors", [None, None])[1]
            status = comp.get("status", {}).get("type", {}).get("description", "")
            lines.append(f"{away.get('team',{}).get('displayName','')} {away.get('score','0')} - "
                         f"{home.get('team',{}).get('displayName','')} {home.get('score','0')} ({status})")

    await interaction.followup.send("\n".join(lines) if lines else "No current games found.")

# -----------------------------
# Bot Ready Event
# -----------------------------

@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user}")
    watcher_loop.start()

# -----------------------------
# Run Bot (correct location)
# -----------------------------

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN is None:
    raise ValueError("DISCORD_TOKEN environment variable not set!")

bot.run(TOKEN)
