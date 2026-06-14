# Telegram bridge

Talk to the mesh — and get the daily brief — from the Telegram app. A small
always-on service (`apps/telegram`, console script `mesh-telegram`, compose
service `telegram-bot`) bridges Telegram to the read API:

- **Chat** — any text you send the bot is forwarded to `POST /api/v1/ask` (the
  same grounded, cited Q&A that powers the wiki `/ask` page); the answer comes
  back as a Telegram reply with its coverage badge and source citations.
- **Daily brief** — once a day the bot fetches `GET /api/v1/briefing` and pushes
  the personalized digest to every allow-listed chat. `/brief` asks for one on
  demand.

It uses Telegram **long polling** (the bot dials out to Telegram's servers), so
no inbound port or public URL is needed — it runs behind the Pi's home-network
NAT. The only exposed port (`9110`) is a local liveness healthcheck.

## Design

- **Thin HTTP bridge, no DB.** The bot only calls the read API over HTTP
  (`MeshApiClient` → `/ask`, `/briefing`), so it inherits all the existing
  field-scoping and graceful-degradation behavior and never writes to the mesh.
- **Default-secure allow-list.** `TELEGRAM_ALLOWED_CHAT_IDS` gates every
  interaction. The bot is discoverable by username, so an **empty** allow-list
  authorizes **no one** — an un-listed chat (or `/start`) gets a reply with its
  own chat id and instructions, which is how you bootstrap the list.
- **Idle, not crash-loop, with no token.** With `TELEGRAM_BOT_TOKEN` unset the
  process stays up and healthy (logging a warning) so you can set the token and
  restart cleanly rather than fighting a restart loop.
- **Plain-text rendering.** Answers/briefs are sent as plain text (no
  `parse_mode`) — the answer markdown can contain characters that would trip
  Telegram's strict parser and drop the whole message. Pure formatting lives in
  `mesh_telegram.format` and is unit-tested (`tests/test_telegram.py`).

## Setup

1. **Create a bot.** Message [@BotFather](https://t.me/BotFather) → `/newbot`,
   copy the token.
2. **Set the token** in the deployment's `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-...
   ```
3. **Start it and find your chat id.** `docker compose up -d telegram-bot`, then
   message the bot. With an empty allow-list it replies with your chat id.
4. **Allow-list yourself** and restart:
   ```
   TELEGRAM_ALLOWED_CHAT_IDS=<your-chat-id>
   ```
   ```
   docker compose up -d telegram-bot
   ```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (empty → idle) | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_CHAT_IDS` | (empty → no one) | Comma-separated chat ids allowed to chat + receive the brief |
| `TELEGRAM_BRIEFING_ENABLED` | `true` | Push the daily brief |
| `TELEGRAM_BRIEFING_HOUR` / `_MINUTE` | `13` / `0` | Daily-brief time of day (in `TELEGRAM_TZ`) |
| `TELEGRAM_TZ` | `UTC` | IANA tz name for the brief time (e.g. `America/Vancouver`) |
| `MESH_TELEGRAM_FIELD` | `ai-robotics` | Field the bot scopes chat + briefs to |
| `MESH_TELEGRAM_API_URL` | `http://api:8000` | Read API base URL |
| `MESH_TELEGRAM_WIKI_URL` | (empty) | When set, brief items link to wiki detail pages |
| `HEALTH_HOST` / `HEALTH_PORT` | `0.0.0.0` / `9110` | Liveness healthcheck bind |

## Dependencies on other services

Chat answers come from **research-qa** and the daily brief from
**personalizer**. On a laptop (`make up`) both run by default. On the Raspberry
Pi overlay (`docker-compose.pi.yml`) they sit in the `ui` profile to save RAM —
the bot still runs without them, but chat answers come back "not covered" and
briefs are empty. To make the bot fully useful on the Pi, start them
(RAM permitting):

```
docker compose up -d research-qa personalizer
```
