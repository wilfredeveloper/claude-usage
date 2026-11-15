#!/usr/bin/env python3
"""Where did my Claude plan usage go? Per-machine breakdown since the reset.

  ./claude_usage.py            this machine now, plus every machine on the hub
  ./claude_usage.py --local    this machine only
  ./claude_usage.py --open     also open the HTML page

Reads, on each machine:
  * Claude Code transcripts (~/.claude/projects/**/*.jsonl), which also cover
    OpenBot agent chats because OpenBot drives Claude Code.
  * OpenBot's usage ledger (~/.config/OpenBot/openbot.db), only for runs that
    leave no transcript: scheduled routines and memory reviews.
Run as root to include other users' homes (e.g. /var/lib/openbot on a server).

It cannot see claude.ai on the web, the desktop or mobile apps, or machines
that never ran it. Standard library only; Python 3.8+.
"""
import argparse
import datetime as dt
import glob
import html
import json
import math
import os
import shlex
import socket
import sqlite3
import subprocess
import sys

# Default window start: 10:00 UTC = 1:00 pm EAT, when the plan limit resets.
RESET_UTC_HOUR = 10
REPORT_TZ_OFFSET_H = 3
REPORT_TZ_NAME = "EAT"

# USD per million tokens: input, cache write, cache read, output.
# Opus 4.8 matches OpenBot's list-price ledger exactly; the rest are fitted to it
# or rough. These are weights for comparing sources, not what you are billed.
PRICES = {
    "claude-opus-4-8": (5.0, 10.0, 0.50, 25.0),
    "claude-opus-5-5": (4.0, 8.0, 0.20, 20.0),
    "claude-fable-5-2": (8.0, 16.0, 0.80, 48.0),
    "claude-sonnet-5-5": (2.0, 4.0, 0.25, 8.0),
    "claude-sonnet-5": (2.0, 4.0, 0.25, 8.0),
    "claude-haiku-5-5": (0.2, 0.4, 0.02, 1.0),
}
DEFAULT_PRICE = PRICES["claude-opus-5-5"]


def price_for(model):
    if model in PRICES:
        return PRICES[model]
    for k, v in PRICES.items():
        if model and model.startswith(k):
            return v
    return DEFAULT_PRICE


def cost(r):
    p = price_for(r["model"])
    return (r["in"] * p[0] + r["cw"] * p[1] + r["cr"] * p[2] + r["out"] * p[3]) / 1e6


def default_since():
    now = dt.datetime.now(dt.timezone.utc)
    s = now.replace(hour=RESET_UTC_HOUR, minute=0, second=0, microsecond=0)
    if s > now:
        s -= dt.timedelta(days=1)
    return s


def parse_since(text):
    if not text:
        return default_since()
    t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def iso_z(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


# ---------------------------------------------------------------- collect

def candidate_homes():
    homes = {os.path.expanduser("~")}
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        homes.add(os.path.dirname(os.environ["CLAUDE_CONFIG_DIR"].rstrip("/")))
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        homes.add("/root")
        for pat in ("/home/*", "/var/lib/*", "/opt/*", "/srv/*"):
            homes.update(p for p in glob.glob(pat) if os.path.isdir(p))
    return sorted(homes)


def tidy_path(p, home):
    if p and home and (p == home or p.startswith(home + "/")):
        return "~" + p[len(home):]
    return p or "?"


def openbot_agent_names(con):
    names = {}
    try:
        for aid, js in con.execute("SELECT agent_id, agent_json FROM projection_agents"):
            try:
                names[aid] = json.loads(js).get("name") or aid
            except Exception:
                names[aid] = aid
    except sqlite3.Error:
        pass
    return names


def gather(since, name):
    since_s = iso_z(since)
    since_epoch = since.timestamp()
    records, seen, scanned, skipped = [], set(), [], []
    transcript_sessions = set()
    agent_names = {}

    homes = candidate_homes()
    dbs = []
    for home in homes:
        db = os.path.join(home, ".config", "OpenBot", "openbot.db")
        if os.path.exists(db):
            dbs.append((home, db))
    for home, db in dbs:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            agent_names.update(openbot_agent_names(con))
            con.close()
        except sqlite3.Error:
            pass

    for home in homes:
        base = os.path.join(home, ".claude", "projects")
        if not os.path.isdir(base):
            continue
        if not os.access(base, os.R_OK | os.X_OK):
            skipped.append(base + " (no permission; run as root)")
            continue
        scanned.append(base)
        for f in glob.glob(base + "/**/*.jsonl", recursive=True):
            try:
                if os.stat(f).st_mtime < since_epoch:
                    continue
                fh = open(f, errors="ignore")
            except OSError:
                continue
            rel = os.path.relpath(f, base)
            parts = rel.split(os.sep)
            session = parts[1][:36] if len(parts) > 1 else os.path.basename(f)[:36]
            session = session.replace(".jsonl", "")
            sub = "subagents" in parts
            project_dir = parts[0]
            agent_id = None
            if "OpenBot-Agents-agent-" in project_dir:
                agent_id = "agent-" + project_dir.split("OpenBot-Agents-agent-", 1)[1]
            with fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage")
                    ts = (d.get("timestamp") or "")[:19] + "Z"
                    if not isinstance(u, dict) or ts < since_s:
                        continue
                    key = (msg.get("id"), d.get("requestId"))
                    if key != (None, None):
                        if key in seen:
                            continue
                        seen.add(key)
                    model = msg.get("model") or "?"
                    if model == "<synthetic>":
                        continue
                    transcript_sessions.add(d.get("sessionId") or session)
                    if agent_id:
                        kind = "OpenBot agent"
                        label = agent_names.get(agent_id, agent_id[:14])
                    else:
                        kind = "Claude Code"
                        label = tidy_path(d.get("cwd"), home)
                    records.append({
                        "ts": ts, "model": model, "kind": kind, "label": label,
                        "session": (d.get("sessionId") or session)[:8], "sub": sub,
                        "in": u.get("input_tokens", 0) or 0,
                        "cw": u.get("cache_creation_input_tokens", 0) or 0,
                        "cr": u.get("cache_read_input_tokens", 0) or 0,
                        "out": u.get("output_tokens", 0) or 0,
                    })

    for home, db in dbs:
        scanned.append(db)
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            rows = con.execute(
                "SELECT occurred_at, agent_id, model, session_id, tokens_json "
                "FROM agent_usage_records WHERE occurred_at >= ?", (since_s,)).fetchall()
            con.close()
        except sqlite3.Error as e:
            skipped.append("%s (%s)" % (db, e))
            continue
        for occurred, aid, model, sid, tj in rows:
            sid = sid or ""
            # Conversation turns are already in the transcripts; keep only
            # background runs that leave no transcript.
            if ":" not in sid or sid.split(":", 1)[1] in transcript_sessions:
                continue
            try:
                t = json.loads(tj or "{}")
            except ValueError:
                continue
            what = sid.split(":", 1)[0]
            records.append({
                "ts": (occurred or "")[:19] + "Z", "model": model or "?",
                "kind": "OpenBot " + ("routine" if what == "routine" else what),
                "label": agent_names.get(aid, (aid or "?")[:14]),
                "session": sid.split(":", 1)[1][:8], "sub": False,
                "in": t.get("uncachedInput", 0) or 0, "cw": t.get("cacheCreation", 0) or 0,
                "cr": t.get("cachedInput", 0) or 0, "out": t.get("output", 0) or 0,
            })

    return {
        "machine": name or socket.gethostname(),
        "host": socket.gethostname(),
        "since": since_s,
        "collected_at": iso_z(dt.datetime.now(dt.timezone.utc)),
        "scanned": scanned,
        "skipped": skipped,
        "records": records,
    }


def slug(name):
    return "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")


# ----------------------------------------------------------------- config
# ~/.config/claude-usage/config.json: {"name": ..., "hub": ..., "artifact_url": ...}
# hub is an SSH target (e.g. "mypa" or "root@1.2.3.4") that collects every
# machine's file in HUB_INBOX. It only needs python3; this script sends itself.

CONFIG_PATH = os.path.expanduser("~/.config/claude-usage/config.json")
HUB_INBOX = "/var/lib/claude-usage/inbox"
REPO = os.path.dirname(os.path.abspath(__file__))
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def config_cmd(args):
    cfg = load_config()
    for key in ("name", "hub", "artifact_url"):
        val = getattr(args, key)
        if val is not None:
            if val:
                cfg[key] = val
            else:
                cfg.pop(key, None)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as fh:
        json.dump(cfg, fh, indent=2)
    print(json.dumps(cfg, indent=2))


def ssh(host, command, stdin=None):
    return subprocess.run(["ssh"] + SSH_OPTS + [host, command], input=stdin, check=True,
                          stdout=subprocess.PIPE, text=True).stdout


def push(path, name, hub):
    # Copy under a temp name, then rename, so the hub never reads half a file.
    final = "%s/%s.json" % (HUB_INBOX, slug(name))
    ssh(hub, "mkdir -p %s" % HUB_INBOX)
    subprocess.run(["scp", "-q"] + SSH_OPTS + [path, "%s:%s.part" % (hub, final)], check=True)
    ssh(hub, "mv %s.part %s" % (shlex.quote(final), shlex.quote(final)))


def write_json(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "w") as fh:
        json.dump(obj, fh)
    os.replace(path + ".part", path)


def collect(args):
    cfg = load_config()
    out = gather(parse_since(args.since), args.name or cfg.get("name"))
    path = args.output or os.path.join(REPO, "data", slug(out["machine"]) + ".json")
    write_json(out, path)
    print("%s: %d API calls since %s, about $%.2f at list prices -> %s" % (
        out["machine"], len(out["records"]), out["since"], sum(cost(r) for r in out["records"]), path))
    for s in out["skipped"]:
        print("  skipped:", s, file=sys.stderr)
    hub = args.push or (cfg.get("hub") if args.push_to_hub else None)
    if hub:
        push(path, out["machine"], hub)
        print("pushed to %s:%s" % (hub, HUB_INBOX))


def in_window(paths, since_s):
    keep = []
    for p in sorted(paths):
        try:
            with open(p) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if d.get("since") == since_s:
            keep.append(d)
        else:
            print("ignoring %s: from an earlier reset window" % os.path.basename(p), file=sys.stderr)
    return keep


# -------------------------------------------------------------------- run
# Runs on the hub: collect here, then build the page from every machine's file
# in the inbox for the current window.

def run(args):
    since_s = iso_z(parse_since(args.since))
    me = gather(parse_since(args.since), args.name or load_config().get("name"))
    write_json(me, os.path.join(args.inbox, slug(me["machine"]) + ".json"))
    machines = in_window(glob.glob(os.path.join(args.inbox, "*.json")), since_s)
    if args.output:
        build_report(machines, args.output)
        os.chmod(args.output, 0o644)


# ------------------------------------------------------------------- show
# The default command: this machine's numbers right away, plus every other
# machine's when a hub is configured.

def show(args):
    cfg = load_config()
    since = parse_since(args.since)
    since_s = iso_z(since)
    me = gather(since, args.name or cfg.get("name"))
    mine = os.path.join(REPO, "data", slug(me["machine"]) + ".json")
    write_json(me, mine)
    paths = [mine]
    hub = cfg.get("hub")
    if hub and not args.local:
        try:
            push(mine, me["machine"], hub)
            with open(os.path.abspath(__file__)) as fh:
                ssh(hub, "python3 -I - run --since %s --inbox %s" % (since_s, HUB_INBOX), stdin=fh.read())
            fetched = os.path.join(REPO, "data", "hub")
            os.makedirs(fetched, exist_ok=True)
            for old in glob.glob(os.path.join(fetched, "*.json")):
                os.remove(old)
            subprocess.run(["scp", "-q"] + SSH_OPTS + ["%s:%s/*.json" % (hub, HUB_INBOX), fetched], check=True)
            paths = glob.glob(os.path.join(fetched, "*.json"))
        except (subprocess.CalledProcessError, OSError) as e:
            print("hub %s unreachable (%s); showing this machine only" % (hub, e), file=sys.stderr)
    machines = in_window(paths, since_s)
    page = args.output or os.path.join(REPO, "report", "claude-usage.html")
    os.makedirs(os.path.dirname(page), exist_ok=True)
    machines = build_report(machines, page)

    total = sum(m["usd"] for m in machines)
    print("\nClaude usage since %s %s (%s UTC), estimated at API list prices\n" % (
        local(since_s, "%-I:%M %p").lower(), REPORT_TZ_NAME, since_s[11:16]))
    for m in machines:
        print("  %-24s %9s  %4.0f%%  %5d calls" % (m["machine"][:24], fmt_usd(m["usd"]), m["share"] * 100, m["calls"]))
    print("  %-24s %9s" % ("Total", fmt_usd(total)))
    for m in machines:
        groups = {}
        for r in m["records"]:
            k = "%s | %s" % (r["kind"], r["label"])
            groups[k] = groups.get(k, 0) + r["usd"]
        if groups:
            print("\n  %s, top sources:" % m["machine"])
            for k, v in sorted(groups.items(), key=lambda x: -x[1])[:6]:
                print("    %8s  %s" % (fmt_usd(v), k[:90]))
    print("\npage: %s" % page)
    print("artifact: %s" % (cfg.get("artifact_url") or "none"))
    if args.open:
        subprocess.run(["xdg-open" if sys.platform.startswith("linux") else "open", page])


# ----------------------------------------------------------------- report

def fmt_tok(n):
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%.0fK" % (n / 1e3)
    return str(int(n))


def fmt_usd(x):
    return "$%.2f" % x if x < 100 else "$%.0f" % x


def local(ts, fmt="%H:%M"):
    t = dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S") + dt.timedelta(hours=REPORT_TZ_OFFSET_H)
    return t.strftime(fmt)


def nice_step(maxv, ticks=4):
    if maxv <= 0:
        return 1
    raw = maxv / ticks
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


def short_model(m):
    return m.replace("claude-", "").replace("-", " ", 1)


def report(args):
    machines = []
    for p in args.files:
        with open(p) as fh:
            machines.append(json.load(fh))
    build_report(machines, args.output)
    print("wrote", args.output)


def build_report(machines, output):
    since = min(m["since"] for m in machines)
    collected = max(m["collected_at"] for m in machines)
    for m in machines:
        for r in m["records"]:
            r["usd"] = cost(r)
        m["usd"] = sum(r["usd"] for r in m["records"])
        m["calls"] = len(m["records"])
        for k in ("out", "cw", "cr", "in"):
            m[k] = sum(r[k] for r in m["records"])
    machines.sort(key=lambda m: -m["usd"])
    grand = sum(m["usd"] for m in machines) or 1e-9
    for i, m in enumerate(machines):
        m["color"] = "var(--m%d)" % (i % 5 + 1)
        m["share"] = m["usd"] / grand

    e = html.escape
    parts = []

    # share bar
    seg = "".join(
        '<span class="seg" style="flex:%.6f;background:%s" title="%s %s"></span>' % (
            max(m["usd"], 0.0001), m["color"], e(m["machine"]), fmt_usd(m["usd"]))
        for m in machines)
    legend = "".join(
        '<li><span class="dot" style="background:%s"></span><b>%s</b> <span class="num">%s</span> <span class="muted num">%.0f%%</span></li>' % (
            m["color"], e(m["machine"]), fmt_usd(m["usd"]), m["share"] * 100)
        for m in machines)
    parts.append('<section class="share"><div class="bar">%s</div><ul class="legend">%s</ul></section>' % (seg, legend))

    # machine cards
    cards = []
    for m in machines:
        bykind = {}
        for r in m["records"]:
            bykind[r["kind"]] = bykind.get(r["kind"], 0) + r["usd"]
        kinds = "".join('<li><span>%s</span><span class="num">%s</span></li>' % (e(k), fmt_usd(v))
                        for k, v in sorted(bykind.items(), key=lambda x: -x[1]))
        cards.append(
            '<article class="card"><header><span class="dot" style="background:%s"></span><h3>%s</h3>'
            '<span class="pill">%.0f%%</span></header>'
            '<div class="big num">%s</div>'
            '<dl><div><dt>API calls</dt><dd class="num">%s</dd></div>'
            '<div><dt>Output</dt><dd class="num">%s</dd></div>'
            '<div><dt>Cache writes</dt><dd class="num">%s</dd></div>'
            '<div><dt>Cache reads</dt><dd class="num">%s</dd></div></dl>'
            '<ul class="kinds">%s</ul>%s</article>' % (
                m["color"], e(m["machine"]), m["share"] * 100, fmt_usd(m["usd"]),
                "{:,}".format(m["calls"]), fmt_tok(m["out"]), fmt_tok(m["cw"]), fmt_tok(m["cr"]),
                kinds or '<li><span class="muted">No usage since the reset</span></li>',
                ('<p class="warn">Could not read: %s</p>' % e("; ".join(m["skipped"]))) if m.get("skipped") else ""))
    parts.append('<section class="cards">%s</section>' % "".join(cards))

    # hourly chart (local time)
    start = dt.datetime.strptime(since[:13], "%Y-%m-%dT%H")
    end = dt.datetime.strptime(collected[:13], "%Y-%m-%dT%H")
    hours = []
    h = start
    while h <= end:
        hours.append(h)
        h += dt.timedelta(hours=1)
    grid = {(m["machine"], hh): 0.0 for m in machines for hh in hours}
    for m in machines:
        for r in m["records"]:
            k = (m["machine"], dt.datetime.strptime(r["ts"][:13], "%Y-%m-%dT%H"))
            if k in grid:
                grid[k] += r["usd"]
    totals = [sum(grid[(m["machine"], hh)] for m in machines) for hh in hours]
    ymax = max(totals + [0.01])
    step = nice_step(ymax)
    top = step * (int(ymax / step) + (0 if ymax % step == 0 else 1))
    W, H, L, R, T, B = 760, 260, 48, 12, 14, 30
    pw, ph = W - L - R, H - T - B
    bw = pw / max(len(hours), 1)
    svg = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Estimated usage per hour by machine">' % (W, H)]
    v = 0.0
    while v <= top + 1e-9:
        y = T + ph - v / top * ph
        svg.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, W - R, y, y))
        svg.append('<text x="%d" y="%.1f" class="axis" text-anchor="end">$%s</text>' % (
            L - 6, y + 4, ("%.2f" % v).rstrip("0").rstrip(".") if v else "0"))
        v += step
    for i, hh in enumerate(hours):
        x = L + i * bw + bw * 0.15
        y = T + ph
        for m in machines:
            val = grid[(m["machine"], hh)]
            if val <= 0:
                continue
            hgt = val / top * ph
            y -= hgt
            svg.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"><title>%s %s: %s</title></rect>' % (
                x, y, bw * 0.7, hgt, m["color"], local(hh.strftime("%Y-%m-%dT%H:00:00")), e(m["machine"]), fmt_usd(val)))
        if len(hours) <= 14 or i % 2 == 0:
            svg.append('<text x="%.1f" y="%d" class="axis" text-anchor="middle">%s</text>' % (
                L + i * bw + bw / 2, H - 10, local(hh.strftime("%Y-%m-%dT%H:00:00"), "%-I%p").lower()))
    svg.append("</svg>")
    parts.append('<section><h2>By hour</h2><p class="muted">Estimated usage per hour (%s), stacked by machine.</p><div class="chart">%s</div></section>' % (
        REPORT_TZ_NAME, "".join(svg)))

    # detail tables
    for m in machines:
        groups = {}
        for r in m["records"]:
            g = groups.setdefault((r["kind"], r["label"]), {"usd": 0, "calls": 0, "out": 0, "cw": 0, "cr": 0,
                                                            "models": {}, "first": r["ts"], "last": r["ts"], "subs": 0, "sessions": set()})
            g["usd"] += r["usd"]
            g["calls"] += 1
            for k in ("out", "cw", "cr"):
                g[k] += r[k]
            g["models"][r["model"]] = g["models"].get(r["model"], 0) + 1
            g["first"] = min(g["first"], r["ts"])
            g["last"] = max(g["last"], r["ts"])
            g["subs"] += 1 if r["sub"] else 0
            g["sessions"].add(r["session"])
        rows = []
        for (kind, label), g in sorted(groups.items(), key=lambda x: -x[1]["usd"]):
            share = g["usd"] / (m["usd"] or 1e-9)
            models = ", ".join("%s ×%d" % (short_model(k), v) for k, v in sorted(g["models"].items(), key=lambda x: -x[1]))
            rows.append(
                '<tr><td><div class="src">%s</div><div class="muted small">%s · %d session%s%s</div></td>'
                '<td class="small">%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                '<td class="num">%s</td><td class="num"><b>%s</b></td>'
                '<td><div class="minibar"><span style="width:%.1f%%;background:%s"></span></div></td>'
                '<td class="num small">%s–%s</td></tr>' % (
                    e(label), e(kind), len(g["sessions"]), "" if len(g["sessions"]) == 1 else "s",
                    (" · %d subagent calls" % g["subs"]) if g["subs"] else "",
                    e(models), "{:,}".format(g["calls"]), fmt_tok(g["out"]), fmt_tok(g["cw"]), fmt_tok(g["cr"]),
                    fmt_usd(g["usd"]), share * 100, m["color"], local(g["first"]), local(g["last"])))
        parts.append(
            '<section><h2><span class="dot" style="background:%s"></span>%s</h2>'
            '<p class="muted small">Host %s · collected %s %s</p>'
            '<div class="tablewrap"><table><thead><tr><th>Source</th><th>Models</th><th class="num">Calls</th>'
            '<th class="num">Output</th><th class="num">Cache write</th><th class="num">Cache read</th>'
            '<th class="num">Est.</th><th>Share of machine</th><th class="num">Active</th></tr></thead>'
            '<tbody>%s</tbody></table></div></section>' % (
                m["color"], e(m["machine"]), e(m["host"]), local(m["collected_at"]), REPORT_TZ_NAME,
                "".join(rows) or '<tr><td colspan="9" class="muted">No usage since the reset.</td></tr>'))

    window = "Since %s %s (%s UTC) on %s · collected up to %s %s" % (
        local(since, "%-I:%M %p").lower(), REPORT_TZ_NAME, since[11:16], local(since, "%a %-d %b"),
        local(collected, "%-I:%M %p").lower(), REPORT_TZ_NAME)

    page = TEMPLATE.replace("{{WINDOW}}", e(window)) \
        .replace("{{TOTAL}}", fmt_usd(sum(m["usd"] for m in machines))) \
        .replace("{{CALLS}}", "{:,}".format(sum(m["calls"] for m in machines))) \
        .replace("{{MACHINES}}", str(len(machines))) \
        .replace("{{BODY}}", "\n".join(parts))
    with open(output, "w") as fh:
        fh.write(page)
    return machines


TEMPLATE = """<title>Claude Plan Drawdown</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Space+Grotesk:wght@500;600&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
/* Layout: summary first (share bar, one card per machine), then the hourly chart, then a table per machine. */
:root {
  --bg: #f6f7f9; --surface: #ffffff; --fg: #17191f; --muted: #626875; --line: #e2e5ea;
  --m1: #3b6fd8; --m2: #d9822b; --m3: #2f9e7a; --m4: #9b5bd1; --m5: #c94f6d; --warn: #b4531a;
  --display: "Space Grotesk", "Inter", system-ui, sans-serif;
  --body: "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #111318; --surface: #1a1d24; --fg: #e8eaef; --muted: #9298a5; --line: #2b2f38;
  --m1: #6f9bf0; --m2: #eda15a; --m3: #4fc39d; --m4: #b58ae6; --m5: #e57a95; --warn: #f0a46c; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #111318; --surface: #1a1d24; --fg: #e8eaef; --muted: #9298a5; --line: #2b2f38;
  --m1: #6f9bf0; --m2: #eda15a; --m3: #4fc39d; --m4: #b58ae6; --m5: #e57a95; --warn: #f0a46c; color-scheme: dark }
body { background: var(--bg); color: var(--fg); font-family: var(--body); line-height: 1.5; }
main { max-width: 1080px; margin: 0 auto; padding-block: 32px 56px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 32px; }
h1 { font-family: var(--display); font-weight: 600; font-size: clamp(1.6rem, 4vw, 2.2rem); margin: 0; text-wrap: balance; }
h2 { font-family: var(--display); font-weight: 600; font-size: 1.2rem; margin: 0 0 4px; display: flex; align-items: center; gap: 10px; }
h3 { font-family: var(--display); font-weight: 600; font-size: 1.05rem; margin: 0; }
p { margin: 0; }
.muted { color: var(--muted); }
.small { font-size: 0.8rem; }
.num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
.head { display: grid; gap: 8px; }
.totals { display: flex; flex-wrap: wrap; gap: 6px 20px; color: var(--muted); }
.totals b { color: var(--fg); font-family: var(--mono); font-weight: 500; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex: none; }
.share { display: grid; gap: 12px; }
.bar { display: flex; height: 18px; border-radius: 4px; overflow: hidden; gap: 2px; background: var(--line); }
.seg { min-width: 3px; }
.legend { list-style: none; padding: 0; margin: 0; display: flex; flex-wrap: wrap; gap: 8px 22px; }
.legend li { display: flex; align-items: center; gap: 8px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 18px; display: grid; gap: 12px; align-content: start; }
.card header { display: flex; align-items: center; gap: 10px; }
.pill { margin-left: auto; font-family: var(--mono); font-size: 0.8rem; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); }
.big { font-size: 1.9rem; font-weight: 500; }
dl { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; margin: 0; }
dt { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
dd { margin: 0; }
.kinds { list-style: none; padding: 12px 0 0; margin: 0; border-top: 1px solid var(--line); display: grid; gap: 4px; font-size: 0.88rem; }
.kinds li { display: flex; justify-content: space-between; gap: 12px; }
.warn { color: var(--warn); font-size: 0.8rem; }
.chart { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 12px; margin-top: 12px; overflow-x: auto; }
.chart svg { width: 100%; min-width: 520px; height: auto; display: block; }
.chart .grid { stroke: var(--line); stroke-width: 1; }
.chart .axis { fill: var(--muted); font-family: var(--mono); font-size: 11px; }
.tablewrap { overflow-x: auto; margin-top: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; min-width: 860px; font-size: 0.88rem; }
th { text-align: left; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); font-weight: 500; padding: 10px 12px; border-bottom: 1px solid var(--line); }
td { padding: 10px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
th.num, td.num { text-align: right; white-space: nowrap; }
.src { font-weight: 500; word-break: break-word; }
.minibar { width: 110px; height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; margin-top: 6px; }
.minibar span { display: block; height: 100%; }
.notes { font-size: 0.88rem; color: var(--muted); display: grid; gap: 6px; max-width: 70ch; }
.notes ul { margin: 0; padding-left: 18px; display: grid; gap: 4px; }
</style>
<main>
  <header class="head">
    <h1>Where today's Claude usage went</h1>
    <p class="muted">{{WINDOW}}</p>
    <div class="totals"><span>Estimated <b>{{TOTAL}}</b> at API list prices</span><span><b>{{CALLS}}</b> API calls</span><span><b>{{MACHINES}}</b> machines</span></div>
  </header>
  {{BODY}}
  <section class="notes">
    <h2>How to read this</h2>
    <ul>
      <li>Dollar figures are list-price estimates used as a common weight. A subscription is not billed this way, and Anthropic does not publish how tokens map to the plan percentage.</li>
      <li>Output tokens and cache writes weigh far more than cache reads. A large cache-read count alone is cheap.</li>
      <li>Counted: Claude Code transcripts (including OpenBot agent chats) and OpenBot routines and memory reviews from OpenBot's ledger.</li>
      <li>Not visible: claude.ai on the web, the desktop and mobile apps, and any machine the collector was not run on.</li>
    </ul>
  </section>
</main>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd")
    w = sp.add_parser("show", help="(default) this machine's usage now, plus the hub's machines if configured")
    w.add_argument("--name", help="machine label (default: config name, else hostname)")
    w.add_argument("--since", help="window start, ISO (default: last 10:00 UTC = 1pm EAT)")
    w.add_argument("--local", action="store_true", help="skip the hub; this machine only")
    w.add_argument("--open", action="store_true", help="open the page in a browser")
    w.add_argument("-o", "--output", help="HTML path (default: report/claude-usage.html in the repo)")
    c = sp.add_parser("collect", help="write this machine's usage to JSON (for cron)")
    c.add_argument("--name", help="machine label (default: config name, else hostname)")
    c.add_argument("--since", help="window start, ISO (default: last 10:00 UTC = 1pm EAT)")
    c.add_argument("-o", "--output", help="JSON path (default: data/<name>.json in the repo)")
    c.add_argument("--push", metavar="HOST", help="also copy it to this hub's inbox")
    c.add_argument("--push-to-hub", action="store_true", help="also copy it to the configured hub")
    r = sp.add_parser("report", help="merge JSON files into one HTML page")
    r.add_argument("files", nargs="+")
    r.add_argument("-o", "--output", default="claude-usage.html")
    h = sp.add_parser("run", help="on the hub: collect here and optionally build the page")
    h.add_argument("--name", help="machine label (default: config name, else hostname)")
    h.add_argument("--since", help="window start, ISO (default: last 10:00 UTC = 1pm EAT)")
    h.add_argument("--inbox", default=HUB_INBOX)
    h.add_argument("-o", "--output", help="also build the HTML page here")
    g = sp.add_parser("config", help="show or set name, hub and artifact URL")
    g.add_argument("--name")
    g.add_argument("--hub", help="SSH target of the hub; empty string to remove")
    g.add_argument("--artifact-url", dest="artifact_url", help="artifact that /usage-report updates")
    a = ap.parse_args()
    if a.cmd is None:
        a = ap.parse_args(["show"] + sys.argv[1:])
    {"show": show, "collect": collect, "report": report, "run": run, "config": config_cmd}[a.cmd](a)


if __name__ == "__main__":
    main()
