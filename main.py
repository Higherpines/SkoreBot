import os
import json
import discord
from discord.ext import tasks
from discord import app_commands
import aiohttp
from datetime import datetime, timezone, timedelta
import re

# -----------------------------
# Load config
# -----------------------------
with open("config.json") as f:
    cfg = json.load(f)

CHANNEL_ID = cfg["channel_id"]
SCHOOL = cfg.get("school_name", "South Carolina Gamecocks").strip()
SPORTS = cfg["sports"]
CHECK_INTERVAL = cfg.get("check_interval_seconds", 60)
PRE_GAME_MINUTES = cfg.get("pre_game_minutes", 30)

# -----------------------------
# Discord setup
# -----------------------------
intents = discord.Intents.default()
intents.message_content = False
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# -----------------------------
# State
# -----------------------------
last_updates = {}
last_status = {}
pre_notified = set()
last_articles = set()

# -----------------------------
# Utilities
# -----------------------------
async def fetch_json(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

async def fetch_html(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.text()

def is_gamecocks_name(name):
    n = (name or "").lower()
    return "south carolina gamecocks" in n or n == "south carolina"

def pick_embed_color_for_status(status_name_upper, competitors):
    if status_name_upper == "STATUS_SCHEDULED":
        return discord.Color.blue()
    if status_name_upper == "STATUS_IN_PROGRESS":
        return discord.Color.orange()
    if status_name_upper == "STATUS_FINAL":
        for c in competitors or []:
            team_name = c.get("team", {}).get("displayName", "")
            if is_gamecocks_name(team_name):
                return discord.Color.green() if c.get("winner", False) else discord.Color.red()
        return discord.Color.dark_gray()
    return discord.Color.light_gray()

def build_scoring_embed(sport_name, play):
    team = play.get("team", {}).get("displayName", "Team")
    away_score = play.get("awayScore", "0")
    home_score = play.get("homeScore", "0")
    text = play.get("text", "Scoring play")
    color = discord.Color.green() if is_gamecocks_name(team) else discord.Color.orange()

    emb = discord.Embed(
        title=f"{sport_name} — Scoring Update",
        description=f"**{text}**",
        color=color
    )
    emb.add_field(name="Team", value=team, inline=True)
    emb.add_field(name="Score", value=f"{away_score} - {home_score}", inline=True)
    logo = play.get("team", {}).get("logo")
    if logo:
        emb.set_thumbnail(url=logo)
    emb.set_footer(text="Powered by ESPN API")
    emb.timestamp = datetime.now(timezone.utc)
    return emb

def build_final_embed(sport_name, summary):
    emb = discord.Embed(
        title=f"{sport_name} — Final",
        description=f"Final score",
        color=discord.Color.dark_gray()
    )
    try:
        comps = summary.get("competitions", [])[0].get("competitors", [])
    except Exception:
        comps = []
    status_type = summary.get("status", {}).get("type", {}).get("name", "")
    emb.color = pick_embed_color_for_status(status_type.upper(), comps)
    home_logo = None
    for c in comps:
        team_name = c.get("team", {}).get("displayName", "")
        score = c.get("score", "0")
        emb.add_field(name=team_name, value=str(score), inline=True)
        if is_gamecocks_name(team_name) and c.get("team", {}).get("logo"):
            home_logo = c["team"]["logo"]
        elif not home_logo and c.get("team", {}).get("logo"):
            home_logo = c["team"]["logo"]
    if home_logo:
        emb.set_thumbnail(url=home_logo)
    emb.set_footer(text="Powered by ESPN API")
    emb.timestamp = datetime.now(timezone.utc)
    return emb

def build_upcoming_embed(sport_name, start_dt, delta_minutes):
    emb = discord.Embed(
        title=f"Upcoming: {sport_name}",
        description=f"{SCHOOL} plays in {delta_minutes} minutes.",
        color=discord.Color.blue()
    )
    emb.add_field(name="Starts", value=start_dt.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'), inline=False)
    emb.set_footer(text="Powered by ESPN API")
    emb.timestamp = datetime.now(timezone.utc)
    return emb

def build_news_embed(title, link, sport_name="Gamecocks News"):
    emb = discord.Embed(
        title=f"{sport_name} — News",
        description=title,
        url=link,
        color=discord.Color.blue()
    )
    emb.set_footer(text="Source: ESPN")
    emb.timestamp = datetime.now(timezone.utc)
    return emb

# -----------------------------
# Game Checking Logic
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
        competitors = comp.get("competitors", [])
        if not any(is_gamecocks_name(c.get("team", {}).get("displayName", "")) for c in competitors):
            continue

        summary_url = scoreboard_url.replace("scoreboard", f"summary?event={game_id}")
        try:
            summary = await fetch_json(summary_url)
        except Exception as e:
            print(f"Failed to fetch summary for {game_id}: {e}")
            continue

        scoring = summary.get("scoringPlays", []) or []
        old = last_updates.get(game_id, [])
        if scoring != old:
            new = scoring[len(old):] if len(scoring) >= len(old) else scoring
            for play in new:
                emb = build_scoring_embed(sport_name, play)
                await channel.send(embed=emb)
            last_updates[game_id] = scoring

        status_type = comp.get("status", {}).get("type", {}).get("name", "").upper()
        prev_status = last_status.get(game_id)

        if status_type in ("STATUS_SCHEDULED", "PRE"):
            start_iso = event.get("date")
            if start_iso:
                start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                delta = start_dt - now
                if 0 < delta.total_seconds() <= PRE_GAME_MINUTES * 60:
                    if game_id not in pre_notified:
                        minutes = int(delta.total_seconds() // 60)
                        emb = build_upcoming_embed(sport_name, start_dt, minutes)
                        await channel.send(embed=emb)
                        pre_notified.add(game_id)

        if status_type == "STATUS_FINAL" and prev_status != "STATUS_FINAL":
            final_emb = build_final_embed(sport_name, summary)
            await channel.send(embed=final_emb)

        last_status[game_id] = status_type

# -----------------------------
# Background Loops
# -----------------------------
@tasks.loop(seconds=CHECK_INTERVAL)
async def watcher_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("Channel not found — check CHANNEL_ID in config.json.")
        return
    for sport in SPORTS:
        await check_sport(sport["url"], sport["name"], channel)

@tasks.loop(minutes=15)
async def news_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        return
    url = "https://www.espn.com/college-football/team/_/id/2579/south-carolina-gamecocks"
    try:
        html = await fetch_html(url)
    except Exception as e:
        print("Failed to fetch news:", e)
        return
    articles = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', html)
    for link, title in articles:
        if "story" in link and "gamecocks" in title.lower():
            if link not in last_articles:
                emb = build_news_embed(title.strip(), link)
                await channel.send(embed=emb)
                last_articles.add(link)

# -----------------------------
# Slash Commands
# -----------------------------
@tree.command(name="score", description="Get today's South Carolina Gamecocks score for a sport.")
@app_commands.describe(sport_name="Optional sport name, e.g. 'Women's Basketball'")
async def slash_score(interaction: discord.Interaction, sport_name: str = None):
    await interaction.response.defer()
    sport
