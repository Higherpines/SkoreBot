import os
import json
import re
import discord
from discord.ext import tasks
from discord import app_commands
import aiohttp
from datetime import datetime, timezone, timedelta

# =============================
# Load config
# =============================
with open("config.json") as f:
    cfg = json.load(f)

CHANNEL_ID = cfg["channel_id"]
SCHOOL = cfg.get("school_name", "South Carolina Gamecocks").strip()
SPORTS = cfg["sports"]
CHECK_INTERVAL = cfg.get("check_interval_seconds", 60)
PRE_GAME_MINUTES = cfg.get("pre_game_minutes", 30)
GUILD_ID = cfg.get("guild_id")  # optional, speeds up command sync

# =============================
# Discord setup
# =============================
intents = discord.Intents.default()
intents.message_content = False
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# =============================
# State
# =============================
last_updates = {}     # game_id -> scoring plays list
last_status = {}      # game_id -> last status string
pre_notified = set()  # game_ids notified for pre-game
last_articles = set() # URLs already posted

# =============================
# HTTP utilities (timeouts + logs)
# =============================
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

# =============================
# Helpers
# =============================
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
        description="Final score",
        color=discord.Color.dark_gray()
    )
    comps = []
    try:
        comps = summary.get("competitions", [])[0].get("competitors", [])
    except Exception:
        pass
    status_type = summary.get("status", {}).get("type", {}).get("name", "")
    emb.color = pick_embed_color_for_status(status_type.upper(), comps)

    thumb = None
    for c in comps:
        team_name = c.get("team", {}).get("displayName", "")
        score = c.get("score", "0")
        emb.add_field(name=team_name, value=str(score), inline=True)
        logo = c.get("team", {}).get("logo")
        if is_gamecocks_name(team_name) and logo:
            thumb = logo
        elif not thumb and logo:
            thumb = logo

    if thumb:
        emb.set_thumbnail(url=thumb)
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

# =============================
# Game checking logic
# =============================
async def check_sport(scoreboard_url, sport_name, channel):
    try:
        data = await fetch_json(scoreboard_url)
    except Exception as e:
        print(f"[{sport_name}] Scoreboard fetch failed: {e}")
        return

    for event in data.get("events", []):
        game_id = event.get("id")
        comp = event.get("competitions", [None])[0]
        if comp is None:
            continue

        competitors = comp.get("competitors", [])
        if not any(is_gamecocks_name(c.get("team", {}).get("displayName", "")) for c in competitors):
            continue

        # Scoring plays via summary
        summary_url = scoreboard_url.replace("scoreboard", f"summary?event={game_id}")
        try:
            summary = await fetch_json(summary_url)
        except Exception as e:
            print(f"[{sport_name}] Summary fetch failed ({game_id}): {e}")
            continue

        # New scoring plays
        scoring = summary.get("scoringPlays", []) or []
        old = last_updates.get(game_id, [])
        if scoring != old:
            new = scoring[len(old):] if len(scoring) >= len(old) else scoring
            for play in new:
                emb = build_scoring_embed(sport_name, play)
                await channel.send(embed=emb)
            last_updates[game_id] = scoring

        # Status transitions
        status_type = comp.get("status", {}).get("type", {}).get("name", "").upper()
        prev_status = last_status.get(game_id)

        # Pre-game notice
        if status_type in ("STATUS_SCHEDULED", "PRE"):
            start_iso = event.get("date")
            if start_iso:
                start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                delta = start_dt - now
                if 0 < delta.total_seconds() <= PRE_GAME_MINUTES * 60 and game_id not in pre_notified:
                    minutes = int(delta.total_seconds() // 60)
                    emb = build_upcoming_embed(sport_name, start_dt, minutes)
                    await channel.send(embed=emb)
                    pre_notified.add(game_id)

        # Final score (only once)
        if status_type == "STATUS_FINAL" and prev_status != "STATUS_FINAL":
            final_emb = build_final_embed(sport_name, summary)
            await channel.send(embed=final_emb)

        last_status[game_id] = status_type

# =============================
# Background loops
# =============================
@tasks.loop(seconds=CHECK_INTERVAL)
async def watcher_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        print("[CONFIG] Channel not found — check channel_id.")
        return
    for sport in SPORTS:
        await check_sport(sport["url"], sport["name"], channel)

@tasks.loop(minutes=15)
async def news_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        return

    # ESPN Gamecocks team page (football). You can add more sources if desired.
    url = "https://www.espn.com/college-football/team/_/id/2579/south-carolina-gamecocks"
    try:
        html = await fetch_html(url)
    except Exception as e:
        print(f"[News] Fetch failed: {e}")
        return

    # Simple link/title scrape (may need tweaks if ESPN HTML changes)
    articles = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', html)
    for link, title in articles:
        if "story" in link and "gamecocks" in title.lower():
            if link not in last_articles:
                emb = build_news_embed(title.strip(), link)
                await channel.send(embed=emb)
                last_articles.add(link)

# =============================
# Global app command error handler
# =============================
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"[AppCmdError] {error.__class__.__name__}: {error}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"Command error: {error.__class__.__name__}", ephemeral=True)
        else:
            await interaction.response.send_message(f"Command error: {error.__class__.__name__}", ephemeral=True)
    except Exception as e:
        print(f"[ErrorHandlerFail] {e}")

# =============================
# Slash commands
# =============================
@tree.command(name="ping", description="Simple health check")
async def ping(interaction: discord.Interaction):
    try:
        await interaction.response.send_message("Pong ✅", ephemeral=True)
    except Exception as e:
        print(f"[PING] Failed: {e}")

@tree.command(name="score", description="Get today's South Carolina Gamecocks score for a sport.")
@app_commands.describe(sport_name="Optional sport name, e.g. 'Women's Basketball'")
async def slash_score(interaction: discord.Interaction, sport_name: str = None):
    # Defer immediately to avoid timeouts
    try:
        await interaction.response.defer()
    except Exception as e:
        print("[/score] Defer failed:", e)
        return

    try:
        # Resolve sport
        sport = None
        if sport_name:
            for s in SPORTS:
                if s["name"].lower() == (sport_name or "").lower():
                    sport = s
                    break
        if not sport:
            sport = SPORTS[0]

        today = datetime.now().strftime("%Y%m%d")
        url = f"{sport['url']}?dates={today}"
        data = await fetch_json(url)
        events = data.get("events", [])
        if not events:
            await interaction.followup.send("No games found today.")
            return

        # Find Gamecocks game
        found_comp, found_event = None, None
        for event in events:
            comp = event.get("competitions", [None])[0]
            if not comp:
                continue
            if any(is_gamecocks_name(c.get("team", {}).get("displayName", "")) for c in comp.get("competitors", [])):
                found_comp, found_event = comp, event
                break

        if not found_comp:
            await interaction.followup.send("No South Carolina Gamecocks games found today.")
            return

        away = found_comp.get("competitors", [])[0]
        home = found_comp.get("competitors", [None, None])[1]
        status = found_comp.get("status", {}).get("type", {}).get("name", "").upper()

        status_text = "⏱️ In Progress"
        if status == "STATUS_FINAL":
            status_text = "✅ Final"
        elif status == "STATUS_SCHEDULED":
            status_text = "📅 Scheduled"
        elif status == "STATUS_IN_PROGRESS":
            status_text = "⏱️ Live"

        color = pick_embed_color_for_status(status, found_comp.get("competitors", []))
        title = f"{away.get('team',{}).get('displayName','')} vs {home.get('team',{}).get('displayName','')}"
        desc = f"{away.get('score','0')} - {home.get('score','0')} ({status_text})"

        embed = discord.Embed(title=title, description=desc, color=color)
        embed.set_footer(text="Powered by ESPN API")
        logo = home.get("team", {}).get("logo") or away.get("team", {}).get("logo")
        if logo:
            embed.set_thumbnail(url=logo)

        # If scheduled, add local start time
        if status in ("STATUS_SCHEDULED", "PRE"):
            start_iso = found_event.get("date")
            if start_iso:
                start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                embed.add_field(name="Starts", value=start_dt.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'), inline=False)

        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"[/score] Error: {e}")
        try:
            await interaction.followup.send(f"Error fetching score: {e}", ephemeral=True)
        except Exception as e2:
            print(f"[/score] Followup failed: {e2}")

@tree.command(name="previous", description="Get previous final scores for South Carolina Gamecocks for a sport.")
@app_commands.describe(sport_name="Optional sport name, e.g. 'Women's Basketball'")
async def slash_previous(interaction: discord.Interaction, sport_name: str = None):
    # Defer immediately
    try:
        await interaction.response.defer()
    except Exception as e:
        print("[/previous] Defer failed:", e)
        return

    try:
        # Resolve sport
        sport = None
        if sport_name:
            for s in SPORTS:
                if s["name"].lower() == (sport_name or "").lower():
                    sport = s
                    break
        if not sport:
            sport = SPORTS[0]

        # Loop season dates (Aug 1 -> today)
        start_date = datetime(datetime.now().year, 8, 1)
        today = datetime.now()
        days = (today - start_date).days + 1
        dates_to_check = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(days)]

        games = []
        wins, losses = 0, 0

        for d in dates_to_check:
            season_url = f"{sport['url']}?dates={d}"
            try:
                data = await fetch_json(season_url)
            except Exception:
                continue

            for event in data.get("events", []):
                comp = event.get("competitions", [None])[0]
                if not comp:
                    continue

                if not any(is_gamecocks_name(c.get("team", {}).get("displayName", "")) for c in comp.get("competitors", [])):
                    continue

                status = comp.get("status", {}).get("type", {}).get("name", "").upper()
                if status != "STATUS_FINAL":
                    continue

                away = comp.get("competitors", [])[0]
                home = comp.get("competitors", [None, None])[1]
                date = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))

                # Track wins/losses
                for c in comp.get("competitors", []):
                    if is_gamecocks_name(c.get("team", {}).get("displayName", "")):
                        if c.get("winner", False):
                            wins += 1
                        else:
                            losses += 1

                games.append({
                    "date": date.strftime("%Y-%m-%d"),
                    "away": away.get("team", {}).get("displayName", ""),
                    "away_score": away.get("score", "0"),
                    "away_logo": away.get("team", {}).get("logo", ""),
                    "home": home.get("team", {}).get("displayName", ""),
                    "home_score": home.get("score", "0"),
                    "home_logo": home.get("team", {}).get("logo", "")
                })

        if not games:
            await interaction.followup.send("No completed South Carolina Gamecocks games found this season.")
            return

        latest_games = games[:5]
        embed = discord.Embed(
            title=f"South Carolina Gamecocks — Previous Games ({wins}-{losses})",
            color=discord.Color.red()
        )
        for g in latest_games:
            embed.add_field(
                name=g['date'],
                value=f"🏟️ {g['away']} {g['away_score']} vs {g['home']} {g['home_score']}",
                inline=False
            )

        thumb = latest_games[0]["home_logo"] or latest_games[0]["away_logo"]
        if thumb:
            embed.set_thumbnail(url=thumb)

        embed.set_footer(text="Powered by ESPN API")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"[/previous] Error: {e}")
        try:
            await interaction.followup.send(f"Error fetching previous games: {e}", ephemeral=True)
        except Exception as e2:
            print(f"[/previous] Followup failed: {e2}")

# =============================
# Ready event (sync + start loops)
# =============================
@bot.event
async def on_ready():
    try:
        if isinstance(GUILD_ID, int):
            guild = discord.Object(id=GUILD_ID)
            await tree.sync(guild=guild)
            print(f"[READY] Commands synced to guild {GUILD_ID}")
        else:
            await tree.sync()
            print("[READY] Commands synced globally (may take minutes)")
    except Exception as e:
        print(f"[READY] Sync failed: {e}")

    # Basic sanity checks
    if not isinstance(CHANNEL_ID, int):
        print("[CONFIG] channel_id must be an integer")
    if not SPORTS or not isinstance(SPORTS, list):
        print("[CONFIG] sports must be a non-empty list")

    print(f"[READY] Logged in as {bot.user}")
    watcher_loop.start()
    news_loop.start()

# =============================
# Run bot
# =============================
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if TOKEN is None:
        raise ValueError("DISCORD_TOKEN environment variable not set!")
    bot.run(TOKEN)
