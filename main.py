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
GUILD_ID = cfg.get("guild_id")
PING_STRING = cfg.get("ping_string", "")

# =============================
# Discord setup
# =============================
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# =============================
# State
# =============================
last_updates = {}
last_status = {}
pre_notified = set()
final_posted = set()
last_articles = set()

# =============================
# HTTP utilities
# =============================
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=12)

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
    n = (name or "").lower().strip()
    return (
        "south carolina gamecocks" in n or
        n == "south carolina" or
        n == "gamecocks" or
        n == "sc"
    )

def hi_res_logo(url, size="200"):
    if not url:
        return None
    return re.sub(r"/\d+\.(png|jpg)$", f"/{size}.\\1", url)

def status_text_from_comp(comp):
    status = comp.get("status", {}).get("type", {}).get("name", "").upper()
    mapping = {
        "STATUS_FINAL": "✅ Final",
        "STATUS_SCHEDULED": "📅 Scheduled",
        "STATUS_IN_PROGRESS": "⏱️ Live",
        "PRE": "📅 Scheduled"
    }
    return mapping.get(status, "⏱️ In Progress"), status

def extract_big_image_from_summary(summary):
    if not summary:
        return None
    try:
        header_comp = summary.get("header", {}).get("competitions", [])[0]
        competitors = header_comp.get("competitors", [])
        # Prefer Gamecocks logo
        for c in competitors:
            t = c.get("team", {})
            if is_gamecocks_name(t.get("displayName", "")):
                return hi_res_logo(t.get("logo"), "400")
        # Fallback to first
        if competitors:
            return hi_res_logo(competitors[0].get("team", {}).get("logo"), "400")
    except Exception:
        pass
    return None

def build_matchup_embed(sport_name, comp, summary=None, override_status_text=None):
    away = comp.get("competitors", [])[0]
    home = comp.get("competitors", [None, None])[1]

    title = f"{away.get('team',{}).get('displayName','')} vs {home.get('team',{}).get('displayName','')}"
    status_text, status_upper = status_text_from_comp(comp)
    if override_status_text:
        status_text = override_status_text

    desc = f"{away.get('score','0')} - {home.get('score','0')} ({status_text})"
    # Color: win green, final red if loss, blue otherwise
    color = discord.Color.blue()
    if status_upper == "STATUS_FINAL":
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            if is_gamecocks_name(t.get("displayName", "")):
                color = discord.Color.green() if c.get("winner", False) else discord.Color.red()
                break

    embed = discord.Embed(title=title, description=desc, color=color)
    embed.set_footer(text="Powered by ESPN API")

    # Thumbnail: prefer Gamecocks logo
    gamecocks_logo = None
    opponent_logo = None
    for c in comp.get("competitors", []):
        t = c.get("team", {})
        if is_gamecocks_name(t.get("displayName", "")):
            gamecocks_logo = t.get("logo")
        else:
            opponent_logo = t.get("logo")
    thumb = hi_res_logo(gamecocks_logo, "200") or hi_res_logo(opponent_logo, "200")
    if thumb:
        embed.set_thumbnail(url=thumb)

    # Big image
    big_img = extract_big_image_from_summary(summary)
    if not big_img:
        big_img = hi_res_logo(gamecocks_logo, "400") or hi_res_logo(opponent_logo, "400")
    if big_img:
        embed.set_image(url=big_img)

    return embed

def build_scoring_embed_espn_style(sport_name, play, comp, summary=None):
    team = play.get("team", {}).get("displayName", "Team")
    away_score = play.get("awayScore", "0")
    home_score = play.get("homeScore", "0")
    text = play.get("text", "Scoring play")

    status_text, _ = status_text_from_comp(comp)
    desc = f"{away_score} - {home_score} ({status_text})\n\n{text}"

    color = discord.Color.green() if is_gamecocks_name(team) else discord.Color.orange()
    embed = discord.Embed(title=f"{sport_name} — Scoring Update", description=desc, color=color)
    embed.set_footer(text="Powered by ESPN API")

    # Thumbnail: prefer Gamecocks logo if opponent scored
    scoring_logo = play.get("team", {}).get("logo")
    sc_logo = None
    opp_logo = None
    for c in comp.get("competitors", []):
        t = c.get("team", {})
        if is_gamecocks_name(t.get("displayName", "")):
            sc_logo = t.get("logo")
        else:
            opp_logo = t.get("logo")
    thumb = hi_res_logo(sc_logo, "200") if not is_gamecocks_name(team) else hi_res_logo(scoring_logo, "200")
    if not thumb:
        thumb = hi_res_logo(sc_logo, "200") or hi_res_logo(opp_logo, "200")
    if thumb:
        embed.set_thumbnail(url=thumb)

    big_img = extract_big_image_from_summary(summary) if summary else None
    if not big_img:
        big_img = hi_res_logo(sc_logo, "400") or hi_res_logo(opp_logo, "400")
    if big_img:
        embed.set_image(url=big_img)

    embed.timestamp = datetime.now(timezone.utc)
    return embed
def build_previous_embed_espan_style(sport_name, games, wins, losses):
    latest = games[0]
    title = f"{SCHOOL} — Previous {sport_name} Games ({wins}-{losses})"
    embed = discord.Embed(title=title, color=discord.Color.red())
    big_logo = latest.get("home_logo") or latest.get("away_logo")
    if big_logo:
        embed.set_image(url=hi_res_logo(big_logo, "400"))
        embed.set_thumbnail(url=hi_res_logo(big_logo, "200"))
    for g in games[:5]:
        embed.add_field(
            name=g['date'],
            value=f"{g['away']} {g['away_score']} vs {g['home']} {g['home_score']}",
            inline=False
        )
    embed.set_footer(text="Powered by ESPN API")
    embed.timestamp = datetime.now(timezone.utc)
    return embed

def build_news_embed_espan_style(title, link, image_url=None):
    emb = discord.Embed(
        title="Gamecocks News",
        description=title,
        url=link,
        color=discord.Color.blue()
    )
    if image_url:
        emb.set_image(url=image_url)
    emb.set_footer(text="Source: ESPN")
    emb.timestamp = datetime.now(timezone.utc)
    return emb

def find_first_img_src(html):
    m = re.search(r'<img[^>]+src="([^"]+)"', html)
    return m.group(1) if m else None

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

        # Summary for richer assets and scoring plays
        summary_url = scoreboard_url.replace("scoreboard", f"summary?event={game_id}")
        try:
            summary = await fetch_json(summary_url)
        except Exception as e:
            print(f"[{sport_name}] Summary fetch failed ({game_id}): {e}")
            summary = None

        # Scoring plays (ESPN-style)
        scoring = (summary or {}).get("scoringPlays", []) or []
        old = last_updates.get(game_id, [])
        if scoring != old:
            new = scoring[len(old):] if len(scoring) >= len(old) else scoring
            for play in new:
                emb = build_scoring_embed_espn_style(sport_name, play, comp, summary)
                await channel.send(embed=emb)
            last_updates[game_id] = scoring

        status_text, status_upper = status_text_from_comp(comp)

        # Pre-game alert
        if status_upper in ("STATUS_SCHEDULED", "PRE"):
            start_iso = event.get("date")
            if start_iso:
                start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                delta = start_dt - now
                if 0 < delta.total_seconds() <= PRE_GAME_MINUTES * 60 and game_id not in pre_notified:
                    minutes = int(delta.total_seconds() // 60)
                    emb = build_matchup_embed(sport_name, comp, summary=summary, override_status_text=f"Starts in {minutes} minutes")
                    if PING_STRING:
                        await channel.send(f"{PING_STRING} Game starting soon!", embed=emb)
                    else:
                        await channel.send(embed=emb)
                    pre_notified.add(game_id)

        # Final alert (deduped)
        if status_upper == "STATUS_FINAL" and game_id not in final_posted:
            final_emb = build_matchup_embed(sport_name, comp, summary=summary, override_status_text="✅ Final")
            await channel.send(embed=final_emb)
            final_posted.add(game_id)

        last_status[game_id] = status_upper

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
    url = "https://www.espn.com/college-football/team/_/id/2579/south-carolina-gamecocks"
    try:
        html = await fetch_html(url)
    except Exception as e:
        print(f"[News] Fetch failed: {e}")
        return
    articles = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', html)
    for link, title in articles:
        if "story" in link and "gamecocks" in title.lower():
            if link not in last_articles:
                # Try to pull an image from the article
                try:
                    article_url = link if link.startswith("http") else f"https://www.espn.com{link}"
                    article_html = await fetch_html(article_url)
                    img = find_first_img_src(article_html)
                except Exception:
                    img = None
                emb = build_news_embed_espan_style(title.strip(), link if link.startswith("http") else f"https://www.espn.com{link}", img)
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
    await interaction.response.send_message("Pong ✅", ephemeral=True)

@tree.command(name="score", description="Get today's South Carolina Gamecocks score for a sport.")
@app_commands.describe(sport_name="Optional sport name, e.g. 'Women's Basketball'")
async def slash_score(interaction: discord.Interaction, sport_name: str = None):
    try:
        await interaction.response.defer()
    except Exception:
        return

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
    try:
        data = await fetch_json(url)
    except Exception as e:
        await interaction.followup.send(f"Error fetching scoreboard: {e}", ephemeral=True)
        return

    events = data.get("events", [])
    if not events:
        await interaction.followup.send("No games found today.")
        return

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

    # Fetch summary for richer assets
    summary_url = sport['url'].replace("scoreboard", f"summary?event={found_event.get('id')}")
    try:
        found_summary = await fetch_json(summary_url)
    except Exception:
        found_summary = None

    status_text, status_upper = status_text_from_comp(found_comp)
    embed = build_matchup_embed(sport["name"], found_comp, summary=found_summary, override_status_text=status_text)

    # If scheduled, add local start time
    if status_upper in ("STATUS_SCHEDULED", "PRE"):
        start_iso = found_event.get("date")
        if start_iso:
            start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            embed.add_field(name="Starts", value=start_dt.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z'), inline=False)

    await interaction.followup.send(embed=embed)

@tree.command(name="previous", description="Get previous final scores for South Carolina Gamecocks for a sport.")
@app_commands.describe(sport_name="Optional sport name, e.g. 'Women's Basketball'")
async def slash_previous(interaction: discord.Interaction, sport_name: str = None):
    try:
        await interaction.response.defer()
    except Exception:
        return

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

            status_text, status_upper = status_text_from_comp(comp)
            if status_upper != "STATUS_FINAL":
                continue

            away = comp.get("competitors", [])[0]
            home = comp.get("competitors", [None, None])[1]
            date = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))

            # Track wins/losses
            for c in comp.get("competitors", []):
                t = c.get("team", {})
                if is_gamecocks_name(t.get("displayName", "")):
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

    embed = build_previous_embed_espan_style(sport["name"], games, wins, losses)
    await interaction.followup.send(embed=embed)

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
