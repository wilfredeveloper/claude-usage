# claude-usage

Where did my Claude plan usage go? One command shows Claude usage since the last limit reset (1 pm EAT), per machine and per source: Claude Code sessions, OpenBot agents, and OpenBot routines.

## Set up a machine

```bash
git clone git@github.com:wilfredeveloper/claude-usage.git ~/tools/claude-usage
cd ~/tools/claude-usage && ./setup.sh
```

`setup.sh` asks for a name for this machine and the hub (default `mypa`). It then:

1. Saves both to `~/.config/claude-usage/config.json`.
2. Adds a cron job that pushes this machine's usage to the hub every 30 minutes.
3. Adds the `/usage-report` command to Claude Code.
4. Prints the current numbers.

## Daily use

```bash
./claude_usage.py           # this machine now, plus every machine on the hub
./claude_usage.py --open    # also open the HTML page
./claude_usage.py --local   # this machine only, no hub
```

In Claude Code, `/usage-report` runs the same thing and publishes the page as an artifact, always to the same link.

## How it works

- **Collect.** Each machine reads its Claude Code transcripts (`~/.claude/projects`, which include OpenBot agent chats) and OpenBot's ledger (`~/.config/OpenBot/openbot.db`) for routine runs that leave no transcript. Run as root to include other users, such as `openbot` on a server.
- **Hub.** Machines copy a JSON file to `/var/lib/claude-usage/inbox` on the hub over SSH. The hub only needs `python3`; the script sends itself when it needs the hub to collect.
- **Report.** The page shows each machine's share, an hourly chart, and every source with its models and tokens.

The hub also collects its own usage on a schedule. On the hub, as root:

```
5,35 * * * * cd /var/lib/claude-usage && /usr/bin/python3 -I /opt/claude-usage/claude_usage.py run >> /var/lib/claude-usage/cron.log 2>&1  # claude usage report
```

with `/opt/claude-usage/claude_usage.py` copied from this repo and a name set once: `python3 claude_usage.py config --name "Server (mypa)"`.

## Reading the numbers

- Dollar figures are list-price estimates used as a common weight. A subscription is not billed this way, and Anthropic does not publish how tokens map to the plan percentage. Prices are in `PRICES` at the top of the script.
- Output tokens and cache writes weigh far more than cache reads.
- It cannot see claude.ai on the web, the desktop or mobile apps, or machines that never ran it.
- Change the reset time with `RESET_UTC_HOUR` and the display timezone with `REPORT_TZ_*`.
