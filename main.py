
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

TOKEN = cfg["token"]
CHANNEL_ID = cfg["channel_id"]
SCHOOL = cfg.get("school_name", "South Carolina")
SPORTS = cfg["sports"]
CHECK_INTERVAL = cfg.get("check_interval_seconds", 60)
PRE_GAME_MINUTES = cfg.get("pre_game_minutes", 30)

intents = discord.Intents.default()
intents.message_content = False  # not needed for slash commands and embeds
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# state
last_updates = {}       # game_id -> last scoring plays list
last_status = {}        # game_id -> last game status (e.g., 'in'/'pre'/'completed')
pre_notified = set()    # game_id set we already sent pre-game notice for

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

def build_final_summary_embed(sport_name, summary):
    emb = discord.Embed(title=f"{sport_name} — Final Score", color=0x1a8cff)
    comp = summary.get("header", {})
    # try to grab competitors and linescore
    try:
        competition = summary.get("boxscore", {}).get("game", {}) or {}
    except Exception:
        competition = {}
    # fallback simple representation
    away = summary.get("competitors", [])[0] if summary.get("competitors") else None
    home = summary.get("competitors", [None, None])[1] if len(summary.get("competitors", []))>1 else None
    if away and home:
        emb.add_field(name=away.get("team", {}).get("displayName","Away"), value=away.get("score","0"), inline=True)
        emb.add_field(name=home.get("team", {}).get("displayName","Home"), value=home.get("score","0"), inline=True)
    emb.description = summary.get("game", {}).get("status", {}).get("type", {}).get("description", "") or ""
    emb.timestamp = datetime.now(timezone.utc)
    return emb

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
        # Determine if our school is playing
        found = False
        for c in competitors:
            team_name = c.get("team", {}).get("displayName", "")
            if SCHOOL.lower() in team_name.lower():
                found = True
                break
        if not found:
            continue

        # Fetch summary for details
        summary_url = scoreboard_url.replace("scoreboard", f"summary?event={game_id}")
        try:
            summary = await fetch_json(summary_url)
        except Exception as e:
            print(f"Failed to fetch summary for {game_id}: {e}")
            continue

        # scoring plays handling
        scoring = summary.get("scoringPlays", []) or []
        old = last_updates.get(game_id, [])
        if scoring != old:
            # find new plays (keep stable ordering)
            new = scoring[len(old):] if len(scoring) >= len(old) else scoring
            for play in new:
                emb = build_embed_for_play(sport_name, play)
                await channel.send(embed=emb)
            last_updates[game_id] = scoring

        # detect status transitions for pre-game and final
        status_type = comp.get("status", {}).get("type", {}).get("name", "").lower()
        # normalizing: 'pre' -> pre-game, 'in' -> live, 'post' or 'complete' -> completed
        prev = last_status.get(game_id)
        # pre-game notification
        if status_type == "pre" or status_type == "scheduled" or comp.get("status", {}).get("type", {}).get("description","").lower().startswith("scheduled") :
            # parse start time
            start = comp.get("status", {}).get("type", {}).get("detail") or event.get("date")
            # use event['date'] if available; it's ISO8601
            start_iso = event.get("date")
            if start_iso:
                start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                delta = start_dt - now
                if 0 < delta.total_seconds() <= PRE_GAME_MINUTES * 60:
                    if game_id not in pre_notified:
                        emb = discord.Embed(title=f"Upcoming: {sport_name}", description=f"{SCHOOL} plays in {int(delta.total_seconds()//60)} minutes.")
                        emb.add_field(name="Starts", value=start_dt.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'), inline=False)
                        await channel.send(embed=emb)
                        pre_notified.add(game_id)

        # final summary
        if status_type in ("post", "completed", "final"):  # various flavors
            if prev and prev not in ("post", "completed", "final"):
                # game just finished -> post final summary embed
                emb = discord.Embed(title=f"{sport_name} — Final", description=f"Final score for {SCHOOL}")
                # attempt to pull competitor scores
                try:
                    comps = summary.get("competitions", [])[0].get("competitors", [])
                    for c in comps:
                        emb.add_field(name=c.get("team", {}).get("displayName",""), value=c.get("score","0"), inline=True)
                except Exception:
                    pass
                await channel.send(embed=emb)

        last_status[game_id] = status_type


@tasks.loop(seconds=CHECK_INTERVAL)
async def watcher_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("Channel not found; make sure CHANNEL_ID is correct and bot is in the guild.")
        return

    for sport in SPORTS:
        await check_sport(sport["url"], sport["nam"], channel)

# Slash command: /score [sport]
@tree.command(name="score", description="Get current live score for USC. Optionally specify sport_name.")
@app_commands.describe(sport_name='Optional sport name, e.g. "College Football"')
async def slash_score(interaction: discord.Interaction, sport_name: str = None):
    await interaction.response.defer()
    # pick sport
    sport = None
    if sport_name:
        for s in SPORTS:
            if s["name"].lower() == sport_name.lower():
                sport = s
                break
    else:
        sport = SPORTS[0]  # default first sport if not provided
    if not sport:
        await interaction.followup.send("Sport not found in config.")
        return
    try:
        data = await fetch_json(sport["url"]) 
    except Exception as e:
        await interaction.followup.send(f"Failed to fetch scoreboard: {e}")
        return
    # find today's/ongoing USC game(s)
    lines = []
    for event in data.get("events", []):
        comp = event.get("competitions", [None])[0]
        if not comp:
            continue
        for c in comp.get("competitors", []):
            if SCHOOL.lower() in c.get("team", {}).get("displayName", "").lower():
                status = comp.get("status", {}).get("type", {}).get("description", "")
                away = comp.get("competitors", [])[0]
                home = comp.get("competitors", [None, None])[1]
                lines.append(f"{away.get('team',{}).get('displayName','')} {away.get('score','0')} - {home.get('team',{}).get('displayName','')} {home.get('score','0')} ({status})" )
    if not lines:
        await interaction.followup.send("No current games found for that sport.")
        return
    msg = "n".join(lines)
    await interaction.followup.send(msg)

# Slash command: /schedule [sport]
@tree.command(name="schedule", description="Get upcoming schedule for USC for a sport.")
@app_commands.describe(sport_name='Optional sport name, e.g. "Mens Basketball"')

async def slash_schedule(interaction: discord.Interaction, sport_name: str = None):
    await interaction.response.defer()
    sport = None
    if sport_name:
        for s in SPORTS:
            if s["name"].lower() == sport_name.lower():
                sport = s
                break
    else:
        sport = SPORTS[0]
    if not sport:
        await interaction.followup.send("Sport not found in config.")
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
        # check if USC is a competitor
        if any(SCHOOL.lower() in c.get("team", {}).get("displayName", "").lower() for c in comp.get("competitors", [])):
            start_iso = event.get("date")
            start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            vs = " vs ".join([c.get("team", {}).get("displayName","") for c in comp.get("competitors", [])])
            lines.append(f"{start_dt.astimezone().strftime('%Y-%m-%d %H:%M %Z')}: {vs}")
    if not lines:
        await interaction.followup.send("No scheduled games found.")
        return
    await interaction.followup.send("n".join(lines))

# Slash command: /nextgame [sport]
@tree.command(name="nextgame", description="Get the next USC game for a sport.")
@app_commands.describe(sport_name='Optional sport name')
async def slash_nextgame(interaction: discord.Interaction, sport_name: str = None):
    await interaction.response.defer()
    sport = None
    if sport_name:
        for s in SPORTS:
            if s["name"].lower() == sport_name.lower():
                sport = s
                break
    else:
        sport = SPORTS[0]
    if not sport:
        await interaction.followup.send("Sport not found in config.")
        return
    try:
        data = await fetch_json(sport["url"]) 
    except Exception as e:
        await interaction.followup.send(f"Failed to fetch scoreboard: {e}")
        return
    upcoming = []
    now = datetime.now(timezone.utc)
    for event in data.get("events", []):
        start_iso = event.get("date")
        start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        comp = event.get("competitions", [None])[0]
        if comp and any(SCHOOL.lower() in c.get("team", {}).get("displayName", "").lower() for c in comp.get("competitors", [])):
            if start_dt > now:
                upcoming.append((start_dt, event))
    if not upcoming:
        await interaction.followup.send("No upcoming games found.")
        return
    upcoming.sort(key=lambda x: x[0])
    start_dt, event = upcoming[0]
    comp = event.get("competitions", [None])[0]
    vs = " vs ".join([c.get("team", {}).get("displayName","") for c in comp.get("competitors", [])])
    await interaction.followup.send(f"Next game: {start_dt.astimezone().strftime('%Y-%m-%d %H:%M %Z')} — {vs}")

@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user} — synced commands.")
    watcher_loop.start()

# Run
bot.run(TOKEN)
