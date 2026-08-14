# MADBOYS / GRASBOYS Pro Clubs Bot

Discord bot for the MADBOYS (and GRASBOYS) EA FC Pro Clubs squads.

Planned features:
- EA Pro Clubs stats integration (latest games, player stats)
- Formation setting + interactive position-preference menus
- Position rotation tracking so people don't get stuck in one spot
- Pro Club build/tips reference commands

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env       # then fill in your real values
python bot.py
```

## Deployment

Hosted on Railway. Push to `main` to deploy — see repo settings in the
Railway dashboard for env vars (set the same keys as `.env.example`).
