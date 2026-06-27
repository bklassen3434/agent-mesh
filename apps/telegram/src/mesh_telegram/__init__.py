"""Telegram bridge to the Agent Mesh read API.

A small always-on service that lets you talk
to the mesh from the Telegram app:

* **Chat** — any text you send is forwarded to ``POST /api/v1/ask`` (the same
  grounded Q&A that powers the wiki ``/ask`` page) and the cited answer comes
  back as a Telegram reply.
* **Daily brief** — once a day it fetches ``GET /api/v1/briefing`` and pushes
  the personalized digest to the allow-listed chats. ``/brief`` requests one
  on demand.

It uses Telegram long polling (the bot dials out to Telegram's servers), so no
inbound port / public URL is required — it runs happily behind the Pi's NAT.
"""
