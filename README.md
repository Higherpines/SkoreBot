
# USC Multi-Sport Bot v2 (Embeds, Slash Commands, Pre-game & Final Summaries)

## Features
- Tracks all sports listed in `config.json`
- Posts pretty Discord embeds for scoring plays and events
- Slash commands: `/score`, `/schedule`, `/nextgame`
- Automatic pre-game notifications (configurable minutes before start)
- Automatic final score summaries when a game completes

## Setup
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Edit `config.json`:
   - Put your bot token in `token`
   - Set an existing `channel_id` for posts
   - Adjust `pre_game_minutes` if you want notifications earlier/later

3. Run the bot:
   ```bash
   python main.py
   ```

## Notes & Next Steps
- The bot uses ESPN's public JSON endpoints. The structure is consistent across college sports but fields may vary slightly by sport.
- Slash commands are synced on start; make sure bot has `applications.commands` scope and you invited it with that scope.
- If you'd like per-sport channels, richer embeds, or web dashboard for managing subscriptions, I can add that.
