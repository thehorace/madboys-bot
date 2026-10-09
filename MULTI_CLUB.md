MADBOYS and GRAYSBOYS are checked automatically by the existing match tracker. The primary ID still comes from `CLUB_ID` (or the legacy `MADBOYS_CLUB_ID`). GRAYSBOYS uses **754785**, from the supplied EA page, on the same `EA_PLATFORM` (`common-gen5` by default).

Commit, push and redeploy once to load this update. No new variables are required for these two clubs. Keep the existing persistent database volume. Each club's first check against empty history quietly saves the available games; later unseen games are announced. No messages are sent for an empty successful check. EA exposes completed matches, so this does not provide live scores.

Use **/admin → Settings → Clubs** to add an EA-verified club or enable/disable tracking without restarting. These controls use the existing private allowlist. Disabling keeps all history, and at least one club must remain enabled. Club IDs and names stay fixed because historical lineup/rotation records use the name. Up to ten clubs on the same platform are supported. `CLUBS_JSON` optionally seeds extra clubs at startup; saved enable/disable choices take precedence. Removing a club from that variable does not erase or disable a saved club: use the private panel.

The tracker keeps the configured polling intervals and processes each enabled club independently. One club's request or processing failure does not prevent checking the next club. Session closure waits for a complete successful check of every enabled club. Private bot status reports each club's last attempt, result and latest recorded match.

Stats commands and newly opened lineup menus automatically follow the most recently active enabled club. **Switch club** in the stats menu changes your personal preference. **/club** also accepts a club from autocomplete; **/club name:auto** restores automatic selection. Existing open views stay bound to their club. None of these viewing controls changes which clubs are monitored.

Results, cards, prompts, milestones and weekly recaps identify the club. Stats, form, opponents, leaderboards, positions and MOTM awards are kept separate. Match storage already uses `(club_id, match_id)`; secondary MOTM keys now include the club ID, while old primary keys remain valid. Schema migrations add club IDs to existing prompts and polls without deleting data.

An evening session can contain games from both clubs. Either club's game resets the inactivity gap. The shared recap labels results by club, and personal recaps retain separate player stats for each club. A player who played for both receives one summary DM for that session. Old primary-only session history remains readable.

Changed files: `config.py`, `.env.example`, `db.py` and `clubs.py` centralize configuration, registry, migration and task-local club scope; `cogs/clubs.py`, `cogs/admin.py`, `bot.py`, `interaction_tracking.py` and `cogs/hub.py` provide controls and bind UI callbacks; `cogs/matchday.py`, `match_data.py`, `match_card.py`, `positions.py`, and the stats/link/lineup/rotation/positions/MOTM/onboarding cogs extend existing features; `cogs/session_reports.py` and `cogs/operations.py` update summaries and status. Regression tests live in `tests/test_multi_club.py`; the older single-club tracker fixture explicitly selects one club.

All **122 local tests passed**, including 15 new multi-club regressions. Verification uses simulated EA responses, real SQLite storage, actual embed/card rendering, and mocked Discord sends. It covers both clubs, switching in both directions, duplicate IDs/restarts, concurrent checks and UI scopes, failures/recovery, preserved legacy polls/prompts, separated MOTM awards and mixed-club sessions. All cogs load and their slash commands serialize. `git diff --check` passed. Live EA access and Discord delivery still require verification after deployment.

Suggested commit title: `Support automatic MADBOYS and GRAYSBOYS tracking`

Suggested commit description:

- Monitor both clubs independently with isolated history, stats, positions and MOTM votes.
- Add private runtime club settings and personal stats/lineup selection.
- Include both clubs in session recaps while keeping player stats separate and sending one DM per player.
- Preserve legacy primary data and quiet initial backfills; keep checking other clubs after failures.
- Validation: 122 local tests passed; Discord deployment still requires a live check.
