# BachThreads

BachThreads is a small Slack app for collecting stray channel messages and reposting
them under the thread where they belong.

## Workflow

1. Mark every stray channel message with `:bookmark:`.
2. Add the `:thread:` reaction to the real top-level message.
3. The app posts the marked messages and any replies below them into the real
   thread in chronological order, removes the `:thread:` reaction, and privately
   reminds the original authors to use Slack replies next time.

Slack's current Later/Saved Posts feature is not available through Slack's API,
so BachThreads uses emoji markers instead.

## Slack App Setup

Create a Slack app with these scopes:

- Bot token scopes: `chat:write`, `reactions:read`, `reactions:write`, `im:write`,
  `users:read`, `channels:history`, `groups:history`
- User token scopes: `reactions:write`

Subscribe the app to these bot events:

- `reaction_added`
- `message.im`

If you want to use `/whitelist ...` as a real Slack slash command, also go to
**Slash Commands** and create:

- Command: `/whitelist`
- Request URL: leave blank for Socket Mode apps, or use `/slack/events` for HTTP
- Short description: `Manage BachThreads whitelist`

The app can run either with Socket Mode or with a public Events API request URL:

- Socket Mode: enable Socket Mode and create an app-level token with
  `connections:write`.
- HTTP: set your Events API request URL to `/slack/events` on the host running
  this app.

## Configuration

Copy `.env.example` to `.env` and fill in real tokens.

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_USER_TOKEN=xoxp-...
SLACK_SIGNING_SECRET=...
SLACK_APP_TOKEN=xapp-...
INITIAL_WHITELIST_USER_IDS=U1234567890,U2345678901
WHITELIST_FILE=data/whitelist.json
QUEUE_FILE=data/message_queue.json
THREAD_EMOJI=thread
MESSAGE_EMOJI=bookmark
```

`SLACK_APP_TOKEN` is only needed for Socket Mode. `INITIAL_WHITELIST_USER_IDS` is
an optional bootstrap list so you have at least one non-admin user who can manage
the bot.

## Whitelist

Only whitelisted users can trigger BachThreads with the `:thread:` reaction.
Whitelist changes can be made by Slack admins/owners or by users who are already
on the whitelist.

Send the bot a DM with:

```text
whitelist list
whitelist add @ada U1234567890
whitelist remove @ada
```

If you configured the Slack slash command, you can also use:

```text
/whitelist list
/whitelist add @ada U1234567890
/whitelist remove @ada
```

Users can be written as Slack mentions, raw Slack user IDs, or exact Slack names.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```
