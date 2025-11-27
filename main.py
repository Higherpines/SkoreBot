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
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

async def fetch_json(url):
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.get(url, headers={"User-Agent": "SkoreBot/1.0"}) as resp:
            resp.raise_for_status()
            return await resp.json()

async def fetch_html(url):
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.get(url, headers={"User-Agent": "SkoreBot/1.0"}) as resp:
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
    if logo
