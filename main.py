import os, json, re, aiohttp, discord
from discord.ext import tasks
from discord import app_commands
from datetime import datetime, timezone, timedelta

# Config
with open("config.json") as f:
    cfg = json.load(f)
CHANNEL_ID = cfg["channel_id"]
SCHOOL = cfg.get("school_name", "South Carolina Gamecocks").strip()
SPORTS = cfg["sports"]
CHECK_INTERVAL = cfg.get("check_interval_seconds", 60)
PRE_GAME_MINUTES = cfg.get("pre_game_minutes", 30)
GUILD_ID = cfg.get("guild_id")
PING_STRING = cfg.get("ping_string", "")

# Discord
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# State
last_updates, last_status = {}, {}
pre_notified, final_posted, last_articles = set(), set(), set()

# Constants
GAMECOCKS_LOGO = "https://a.espncdn.com/i/teamlogos/ncaa/500/2579.png"
SPORT_EMOJIS = {
    "College Football": "🏈",
    "Men's Basketball": "🏀",
    "Women's Basketball": "🏀",
    "Baseball": "⚾",
    "Softball": "🥎",
    "Soccer": "⚽",
}

# HTTP
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=12)
async def fetch_json(url):
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as s:
        async with s.get(url, headers={"User-Agent": "SkoreBot/1.0"}) as r:
            r.raise_for_status()
            return await r.json()
async def fetch_html(url):
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as s:
        async with s.get(url, headers={"User-Agent": "SkoreBot/1.0"}) as r:
            r.raise_for_status()
            return await r.text()

# Helpers
def status_text_from_comp(comp):
    s = comp.get("status", {}).get("type", {}).get("name", "").upper()
    return {
        "STATUS_FINAL": "✅ Final",
        "STATUS_SCHEDULED": "📅 Scheduled",
        "STATUS_IN_PROGRESS": "⏱️ Live",
        "PRE": "📅 Scheduled",
    }.get(s, "⏱️ In Progress"), s

def build_matchup_embed(sport_name, comp, override=None):
    emoji = SPORT_EMOJIS.get(sport_name, "")
    away = comp.get("competitors", [{}])[0]
    home = comp.get("competitors", [None, None])[1]
    status_text, _ = status_text_from_comp(comp)
    if override: status_text = override
    title = f"{emoji} {away.get('team',{}).get('displayName','')} vs {home.get('team',{}).get('displayName','')}"
    desc = f"{away.get('score','0')} - {home.get('score','0')} ({status_text})"
    e = discord.Embed(title=title, description=desc, color=discord.Color.blue())
    e.set_footer(text="Powered by ESPN API")
    e.set_thumbnail(url=GAMECOCKS_LOGO)
    e.set_image(url=GAMECOCKS_LOGO)
    return e

def build_scoring_embed(sport_name, play, comp):
    emoji = SPORT_EMOJIS.get(sport_name, "")
    team = play.get("team", {}).get("displayName", "Team")
    away, home = play.get("awayScore", "0"), play.get("homeScore", "0")
    status_text, _ = status_text_from_comp(comp)
    desc = f"{away} - {home} ({status_text})\n\n{play.get('text','Scoring play')}"
    color = discord.Color.green() if "south carolina" in (team or "").lower() else discord.Color.orange()
    e = discord.Embed(title=f"{emoji} {sport_name} — Scoring Update", description=desc, color=color)
    e.set_footer(text="Powered by ESPN API")
    e.set_thumbnail(url=GAMECOCKS_LOGO)
    e.set_image(url=GAMECOCKS_LOGO)
    e.timestamp = datetime.now(timezone.utc)
    return e

def build_previous_embed(sport_name, games, wins, losses):
    emoji = SPORT_EMOJIS.get(sport_name, "")
    e = discord.Embed(title=f"{emoji} {SCHOOL} — Previous {sport_name} Games ({wins}-{losses})", color=discord.Color.red())
    e.set_thumbnail(url=GAMECOCKS_LOGO)
    e.set_image(url=GAMECOCKS_LOGO)
    for g in games[:5]:
        e.add_field(name=g['date'], value=f"{g['away']} {g['away_score']} vs {g['home']} {g['home_score']}", inline=False)
    e.set_footer(text="Powered by ESPN API")
    e.timestamp = datetime.now(timezone.utc)
    return e

def build_news_embed(title, link):
    e = discord.Embed(title="Gamecocks News", description=title, url=link, color=discord.Color.blue())
    e.set_thumbnail(url=GAMECOCKS_LOGO)
    e.set_image(url=GAMECOCKS_LOGO)
    e.set_footer(text="Source: ESPN")
    e.timestamp = datetime.now(timezone.utc)
    return e

def find_first_img_src(html):
    m = re.search(r'<img[^>]+src="([^"]+)"', html); return m.group(1) if m else None

# Game checking
async def check_sport(url, sport_name, channel):
    try:
        data = await fetch_json(url)
    except Exception as e:
        print(f"[{sport_name}] fetch fail {e}")
        return

    for event in data.get("events", []):
        comp = event.get("competitions", [None])[0]
        if not comp: continue
        # Filter to South Carolina games (by displayName string match)
        if not any("south carolina" in (c.get("team", {}).get("displayName", "").lower()) for c in comp.get("competitors", [])):
            continue

        gid = event.get("id")
        # Scoring plays via summary (optional; still build embeds without summary)
        try:
            summary = await fetch_json(url.replace("scoreboard", f"summary?event={gid}"))
        except:
            summary = None

        scoring = (summary or {}).get("scoringPlays", []) or []
        old = last_updates.get(gid, [])
        if scoring != old:
            new = scoring[len(old):] if len(scoring) >= len(old) else scoring
            for play in new:
                await channel.send(embed=build_scoring_embed(sport_name, play, comp))
            last_updates[gid] = scoring

        status_text, status_upper = status_text_from_comp(comp)

        # Pre-game
        if status_upper in ("STATUS_SCHEDULED", "PRE"):
            start_iso = event.get("date")
            if start_iso:
                start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                delta = start_dt - datetime.now(timezone.utc)
                if 0 < delta.total_seconds() <= PRE_GAME_MINUTES * 60 and gid not in pre_notified:
                    emb = build_matchup_embed(sport_name, comp, override=f"Starts in {int(delta.total_seconds()//60)} minutes")
                    if PING_STRING:
                        await channel.send(f"{PING_STRING} Game starting soon!", embed=emb)
                    else:
                        await channel.send(embed=emb)
                    pre_notified.add(gid)

        # Final (deduped)
        if status_upper == "STATUS_FINAL" and gid not in final_posted:
            await channel.send(embed=build_matchup_embed(sport_name, comp, override="✅ Final"))
            final_posted.add(gid)

        last_status[gid] = status_upper

# Loops
@tasks.loop(seconds=CHECK_INTERVAL)
async def watcher_loop():
    await bot.wait_until_ready()
    ch = bot.get_channel(CHANNEL_ID)
    if ch is None:
        print("[CONFIG] Channel not found — check channel_id")
        return
    for sport in SPORTS:
        await check_sport(sport["url"], sport["name"], ch)

@tasks.loop(minutes=15)
async def news_loop():
    await bot.wait_until_ready()
    ch = bot.get_channel(CHANNEL_ID)
    if ch is None: return
    url = "https://www.espn.com/college-football/team/_/id/2579/south-carolina-gamecocks"
    try:
        html = await fetch_html(url)
    except Exception as e:
        print(f"[News] Fetch failed: {e}")
        return
    articles = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>', html)
    for link, title in articles:
        if "story" in link and "gamecocks" in title.lower():
            article_url = link if link.startswith("http") else f"https://www.espn.com{link}"
            if article_url not in last_articles:
                try:
                    article_html = await fetch_html(article_url)
                    _ = find_first_img_src(article_html)
                except:
                    pass
                await ch.send(embed=build_news_embed(title.strip(), article_url))
                last_articles.add(article_url)

# Errors
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

# Commands
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
    sport = None
    if sport_name:
        for s in SPORTS:
            if s["name"].lower() == (sport_name or "").lower():
                sport = s; break
    if not sport: sport = SPORTS[0]
    today = datetime.now().strftime("%Y%m%d")
    url = f"{sport['url']}?dates={today}"
    try:
        data = await fetch_json(url)
    except Exception as e:
        await interaction.followup.send(f"Error fetching scoreboard: {e}", ephemeral=True)
        return
    events = data.get("events", [])
    if not events:
        await interaction.followup.send("No games found today."); return
    found_comp, found_event = None, None
    for event in events:
        comp = event.get("competitions", [None])[0]
        if not comp: continue
        if any("south carolina" in (c.get("team", {}).get("displayName", "").lower()) for c in comp.get("competitors", [])):
            found_comp, found_event = comp, event; break
    if not found_comp:
        await interaction.followup.send("No South Carolina Gamecocks games found today."); return
    status_text, status_upper = status_text_from_comp(found_comp)
    embed = build_matchup_embed(sport["name"], found_comp, override=status_text)
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
    sport = None
    if sport_name:
        for s in SPORTS:
            if s["name"].lower() == (sport_name or "").lower():
                sport = s; break
    if not sport: sport = SPORTS[0]
    start_date = datetime(datetime.now().year, 8, 1)
    today = datetime.now()
    days = (today - start_date).days + 1
    dates = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(days)]
    games, wins, losses = [], 0, 0
    for d in dates:
        season_url = f"{sport['url']}?dates={d}"
        try:
            data = await fetch_json(season_url)
        except Exception:
            continue
        for event in data.get("events", []):
            comp = event.get("competitions", [None])[0]
            if not comp: continue
            if not any("south carolina" in (c.get("team", {}).get("displayName", "").lower()) for c in comp.get("competitors", [])):
                continue
            status_text, status_upper = status_text_from_comp(comp)
            if status_upper != "STATUS_FINAL": continue
            away = comp.get("competitors", [])[0]
            home = comp.get("competitors", [None, None])[1]
            date = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            for c in comp.get("competitors", []):
                t = c.get("team", {})
                if "south carolina" in (t.get("displayName","").lower()):
                    if c.get("winner", False): wins += 1
                    else: losses += 1
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
        await interaction.followup.send("No completed South Carolina Gamecocks games found this season."); return
    embed = build_previous_embed(sport["name"], games, wins, losses)
    await interaction.followup.send(embed=embed)

# Ready
@bot.event
async def on_ready():
    try:
        if isinstance(GUILD_ID, int):
            guild = discord.Object(id=GUILD_ID)
            await tree.sync(guild=guild); print(f"[READY] Commands synced to guild {GUILD_ID}")
        else:
            await tree.sync(); print("[READY] Commands synced globally (may take minutes)")
    except Exception as e:
        print(f"[READY] Sync failed: {e}")
    if not isinstance(CHANNEL_ID, int): print("[CONFIG] channel_id must be an integer")
    if not SPORTS or not isinstance(SPORTS, list): print("[CONFIG] sports must be a non-empty list")
    print(f"[READY] Logged in as {bot.user}")
    watcher_loop.start(); news_loop.start()

# Run
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if TOKEN is None: raise ValueError("DISCORD_TOKEN environment variable not set!")
    bot.run(TOKEN)
