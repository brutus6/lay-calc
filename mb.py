#!/usr/bin/env python3
"""Tom's matched betting helper: lay calculator + free odds matcher.

  mb calc q   10 2.88 3.1          qualifying bet (stake, back odds, lay odds)
  mb calc snr 30 3.75 3.85         free bet, stake NOT returned (most UK free bets)
  mb calc sr  10 4.0  4.2          free bet, stake returned
      options: --ex betfair|smarkets  (commission from config)  or  --comm 5
  mb calc 2up 25 2.5 24.80 2.52 1.10  2UP trade-out: bookie stake, back odds, your lay stake, lay odds,
                                   in-play back odds now (team is 2 goals up, bookie already paid out)
  mb lay "arsenal"                 live Smarkets lay prices for matches with that name (no key needed)
  mb match [--type q|snr] [--stake 10] [--min-odds 1.5] [--max-odds 0] [--bookies wh,paddy]
           [--days 3] [--top 25] [--fresh]
                                   best bookie back vs exchange lay (needs The Odds API key)
  mb quota                         The Odds API credits left (from the last call)

Config (secrets, never synced): ~/.config/oddsmatcher/config.json
"""
import argparse, difflib, json, os, re, sys, time, unicodedata
from datetime import datetime, timedelta, timezone

import requests

CONF_DIR = os.path.expanduser("~/.config/oddsmatcher")
CONF_FILE = os.path.join(CONF_DIR, "config.json")
CACHE_DIR = os.path.expanduser("~/.cache/oddsmatcher")
UA = {"User-Agent": "oddsmatcher/0.1 (personal use)", "Accept": "application/json"}
SMK = "https://api.smarkets.com/v3"
ODDS = "https://api.the-odds-api.com/v4"

DEFAULT_CONF = {
    "odds_api_key": "",
    # Commission in %. Taken from Tom's sheet: Betfair lays work out at 5%, Smarkets at 0%.
    "commission": {"betfair": 5.0, "smarkets": 0.0},
    # Competitions to scan (The Odds API sport keys). Each one costs 1 credit per market per refresh.
    # Mainstream competitions only (Tom's rule, 2026-11-22): good exchange liquidity, and big
    # televised games look like normal punting, so accounts last longer before being gubbed.
    "sports": ["soccer_epl", "soccer_efl_champ", "soccer_uefa_champs_league",
               "soccer_uefa_europa_league", "soccer_fa_cup", "soccer_england_efl_cup",
               "soccer_spain_la_liga", "soccer_italy_serie_a",
               "soccer_germany_bundesliga", "soccer_france_ligue_one", "soccer_spl"],
    # Hide matches with less than this (in £) waiting at the best lay price.
    "min_lay_liquidity": 100,
    # Markets from The Odds API. "h2h" = match result. Add "h2h_lay" for Betfair lay prices (+1 credit
    # per competition). Off by default: Smarkets at 0% beats Betfair at 2-5% almost every time.
    "markets": ["h2h"],
    "cache_minutes": 30,
}

# --------------------------------------------------------------------------- config / cache

def load_conf():
    conf = json.loads(json.dumps(DEFAULT_CONF))
    if os.path.exists(CONF_FILE):
        with open(CONF_FILE) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(conf.get(k), dict):
                conf[k].update(v)
            else:
                conf[k] = v
    return conf


def cache_get(name, max_age_min):
    p = os.path.join(CACHE_DIR, name)
    if max_age_min and os.path.exists(p) and time.time() - os.path.getmtime(p) < max_age_min * 60:
        with open(p) as f:
            return json.load(f)
    return None


def cache_put(name, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, name), "w") as f:
        json.dump(data, f)

# --------------------------------------------------------------------------- lay calculator

def lay_calc(kind, stake, back, lay, comm_pct):
    """Return dict with lay stake, liability and profit if back wins / lay wins."""
    c = comm_pct / 100.0
    if lay <= c + 1:
        raise ValueError("lay odds too low")
    if kind == "q":            # normal money bet
        L = stake * back / (lay - c)
        back_wins = stake * (back - 1) - L * (lay - 1)
        lay_wins = L * (1 - c) - stake
    elif kind == "snr":        # free bet, stake not returned
        L = stake * (back - 1) / (lay - c)
        back_wins = stake * (back - 1) - L * (lay - 1)
        lay_wins = L * (1 - c)
    elif kind == "sr":         # free bet, stake returned
        L = stake * back / (lay - c)
        back_wins = stake * back - L * (lay - 1)
        lay_wins = L * (1 - c)
    else:
        raise ValueError("type must be q, snr or sr")
    out = {"lay_stake": L, "liability": L * (lay - 1), "back_wins": back_wins, "lay_wins": lay_wins,
           "profit": min(back_wins, lay_wins)}
    if kind != "q":
        out["retention_pct"] = 100 * out["profit"] / stake
    return out


def comm_for(conf, ex, override=None):
    if override is not None:
        return float(override)
    return float(conf["commission"].get(ex, 5.0))

# --------------------------------------------------------------------------- name matching

ALIASES = {"utd": "united", "man": "manchester", "spurs": "tottenham", "nottm": "nottingham",
           "wolves": "wolverhampton", "st": "saint", "psg": "paris saint germain"}
DROP = {"fc", "afc", "cf", "sc", "ac", "the", "de", "club", "calcio", "hotspur", "and", "fk", "sk", "bk"}


def norm(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s.replace("&", " and "))
    words = [ALIASES.get(w, w) for w in s.split()]
    return " ".join(w for w in " ".join(words).split() if w not in DROP)


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()

# --------------------------------------------------------------------------- Smarkets

def smk_get(path, **params):
    for attempt in range(3):
        r = requests.get(SMK + path, params=params, headers=UA, timeout=25)
        if r.status_code == 429:
            time.sleep(2 + attempt * 3)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def smk_events(days, fresh=False):
    key = f"smk_events_{days}.json"
    ev = None if fresh else cache_get(key, 15)
    if ev is not None:
        return ev
    now = datetime.now(timezone.utc)
    params = {"state": "upcoming", "type": "football_match", "limit": 300, "sort": "start_datetime,id",
              "start_datetime_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "start_datetime_max": (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    ev, path = [], "/events/"
    for _ in range(40):
        d = smk_get(path, **params)
        ev += [{"id": e["id"], "name": e["name"], "start": e["start_datetime"]} for e in d["events"]]
        nxt = (d.get("pagination") or {}).get("next_page")
        if not nxt:
            break
        # next_page is a query string like "?pagination_last_id=...&..."
        params = dict(p.split("=", 1) for p in nxt.lstrip("?").split("&") if "=" in p)
        params = {k: requests.utils.unquote(v) for k, v in params.items()}
    cache_put(key, ev)
    return ev


def chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


BY_NAME = ("CORRECT_SCORE", "HALF_FULL", "HALF_TIME_CORRECT_SCORE")


def smk_prices(event_ids, want=("WINNER_3_WAY",)):
    """{event_id: {market_name: {contract_type_or_name: {'lay': odds, 'liq': £, 'back': odds}}}}"""
    out = {}
    if not event_ids:
        return out
    markets = []
    for ch in chunks(list(event_ids), 20):
        markets += smk_get("/events/" + ",".join(ch) + "/markets/")["markets"]
    markets = [m for m in markets if (m.get("market_type") or {}).get("name") in want]
    for ch in chunks(markets, 20):
        ids = ",".join(m["id"] for m in ch)
        contracts = smk_get(f"/markets/{ids}/contracts/")["contracts"]
        quotes = smk_get(f"/markets/{ids}/quotes/")
        mname = {m["id"]: (m["event_id"], m["name"], (m.get("market_type") or {})) for m in ch}
        for c in contracts:
            eid, mn, mt = mname[c["market_id"]]
            q = quotes.get(c["id"], {})
            bids = sorted(q.get("bids", []), key=lambda b: -b["price"])
            offers = sorted(q.get("offers", []), key=lambda b: b["price"])
            ctype = (c.get("contract_type") or {}).get("name") or c["name"]
            if mt.get("name") in BY_NAME:  # many contracts share one type here (e.g. every score)
                ctype = c["name"]
            entry = {"name": c["name"], "lay": None, "liq": 0.0, "back": None}
            # A Smarkets price is implied probability in basis points. Laying means selling
            # into the best bid; the bid's backer stake (quantity x price) is what you can lay.
            if bids:
                entry["lay"] = round(10000 / bids[0]["price"], 2)
                entry["liq"] = round(bids[0]["quantity"] * bids[0]["price"] / 1e8, 2)
            if offers:
                entry["back"] = round(10000 / offers[0]["price"], 2)
            out.setdefault(eid, {}).setdefault(mn, {})[ctype] = entry
    return out


def find_smk_event(events, home, away, start_iso):
    t = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    best, score = None, 0.0
    for e in events:
        et = datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
        if abs((et - t).total_seconds()) > 3 * 3600:
            continue
        parts = re.split(r"\s+vs?\.?\s+", e["name"], maxsplit=1)
        if len(parts) != 2:
            continue
        s = (sim(home, parts[0]) + sim(away, parts[1])) / 2
        if s > score:
            best, score = e, s
    return best if score >= 0.72 else None

# --------------------------------------------------------------------------- The Odds API

QUOTA = os.path.join(CACHE_DIR, "quota.json")


def odds_api(conf, sport, fresh=False):
    key = f"odds_{sport}_{'-'.join(conf['markets'])}.json"
    data = None if fresh else cache_get(key, conf["cache_minutes"])
    if data is not None:
        return data
    if not conf.get("odds_api_key"):
        sys.exit("No Odds API key yet. Put it in ~/.config/oddsmatcher/config.json as \"odds_api_key\".")
    r = requests.get(f"{ODDS}/sports/{sport}/odds", timeout=30, headers=UA, params={
        "apiKey": conf["odds_api_key"], "regions": "uk", "markets": ",".join(conf["markets"]),
        "oddsFormat": "decimal", "dateFormat": "iso"})
    q = {"remaining": r.headers.get("x-requests-remaining"), "used": r.headers.get("x-requests-used"),
         "at": datetime.now().strftime("%F %T")}
    cache_put("quota.json", q)
    if r.status_code == 422 or r.status_code == 404:   # sport out of season / unknown
        cache_put(key, [])
        return []
    r.raise_for_status()
    data = r.json()
    cache_put(key, data)
    return data

# --------------------------------------------------------------------------- commands

def money(x):
    return f"-£{abs(x):.2f}" if x < 0 else f"£{x:.2f}"


def twoup(stake, back, lay_stake, lay, inplay):
    """Bookie has paid out early. Back the team on the exchange at in-play odds to cancel the open lay.
    Equal-profit back stake = lay stake x lay odds / in-play odds (exchange side then loses a little
    either way, so no commission is due)."""
    x = lay_stake * lay / inplay
    bookie = stake * (back - 1)
    win = bookie - lay_stake * (lay - 1) + x * (inplay - 1)
    lose = bookie + lay_stake - x
    ride_win, ride_lose = bookie - lay_stake * (lay - 1), bookie + lay_stake
    return {"back_stake": x, "win": win, "lose": lose, "ride_win": ride_win, "ride_lose": ride_lose}


def acca_calc(kind, stake, legs, comm_pct, won=0, sunk=None):
    """Lay an acca one leg at a time, each leg just before it kicks off, so every outcome ends the same.
    legs = [(back, lay), ...] in kick-off order. Working back from the last leg:
      last lay  = (bookie win - bookie loss) / (lay - c)
      earlier   = next lay x (1 - c) / (lay - c)
    won = how many legs have already won; sunk = liability already lost on their lays (default: the
    recommended stakes at the odds given). Remaining legs use the lay odds given now, so re-run with
    fresh odds as each leg wins."""
    c = comm_pct / 100
    acca = 1.0
    for b, _ in legs:
        acca *= b
    if kind == "q":
        win, lose = stake * (acca - 1), -stake
    elif kind == "snr":
        win, lose = stake * (acca - 1), 0.0
    else:
        win, lose = stake * acca, 0.0
    n = len(legs)
    lays = [0.0] * n
    lays[-1] = (win - lose) / (legs[-1][1] - c)
    for k in range(n - 2, -1, -1):
        lays[k] = lays[k + 1] * (1 - c) / (legs[k][1] - c)
    liab = [lays[i] * (legs[i][1] - 1) for i in range(n)]
    if sunk is None:
        sunk = sum(liab[:won])
    result = (lose + lays[won] * (1 - c) - sunk) if won < n else (win - sunk)
    return {"acca_odds": acca, "lays": lays, "liabilities": liab, "result": result,
            "max_liability": max(liab[won:]) if won < n else 0.0, "sunk": sunk,
            "retention_pct": None if kind == "q" else 100 * result / stake}


def cmd_acca(a, conf):
    legs = []
    for part in a.legs.split(","):
        try:
            b, l = (float(x) for x in part.strip().split("/"))
        except ValueError:
            sys.exit(f"Bad leg '{part}': write each leg as BACK/LAY, e.g. 2.20/2.30")
        legs.append((b, l))
    if not 0 <= a.won < len(legs):
        sys.exit("--won must be less than the number of legs")
    comm = comm_for(conf, a.ex, a.comm)
    r = acca_calc(a.type, a.stake, legs, comm, a.won, a.sunk)
    label = {"q": "Qualifying acca", "snr": "Free bet acca (stake not returned)", "sr": "Free bet acca (stake returned)"}[a.type]
    print(f"{label}: £{a.stake:g} @ {r['acca_odds']:.2f} ({len(legs)} legs), lay one leg at a time on {a.ex} ({comm:g}% commission)")
    if a.won:
        done = "Leg 1 has" if a.won == 1 else f"Legs 1-{a.won} have"
        print(f"  {done} already won; liability lost on the lay so far: {money(r['sunk'])}")
    for i, (b, l) in enumerate(legs):
        if i < a.won:
            print(f"  Leg {i+1}: won")
            continue
        when = "now" if i == a.won else f"only if leg {i} wins"
        print(f"  Leg {i+1}: back {b} / lay {l}   LAY {money(r['lays'][i])}   liability {money(r['liabilities'][i])}   ({when})")
    print(f"  Whatever happens you end on {money(r['result'])}"
          + (f"  ({r['retention_pct']:.1f}% of the free bet)" if r["retention_pct"] is not None else "  (qualifying loss)"))
    print(f"  Biggest single liability {money(r['max_liability'])}. Stop laying as soon as a leg loses.")
    print("  Re-check the lay odds before each leg and re-run with --won N --sunk £ if they have moved.")


def boost_calc(stake, odds, lay, comm_pct, boost_pct=0.0):
    """Arb a price boost (or any bookie price above the lay price). boost_pct is added to the winnings
    part of the odds, e.g. 30% turns 3.50 into 1 + 2.50 x 1.3 = 4.25. Use 0 if `odds` is already boosted.
    The lay is sized so the bookie and exchange outcomes are equal."""
    c = comm_pct / 100
    boosted = 1 + (odds - 1) * (1 + boost_pct / 100)
    L = stake * boosted / (lay - c)
    profit = L * (1 - c) - stake
    return {"boosted": boosted, "lay_stake": L, "liability": L * (lay - 1), "profit": profit, "pct": 100 * profit / stake}


def cmd_boost(a, conf):
    comm = comm_for(conf, a.ex, a.comm)
    rows = []
    for part in a.sel.split(","):
        try:
            name, prices = part.rsplit(":", 1) if ":" in part else (f"Selection {len(rows)+1}", part)
            o, l = (float(x) for x in prices.strip().split("/"))
        except ValueError:
            sys.exit(f"Bad selection '{part}': write NAME:BOOKIE_ODDS/LAY_ODDS, e.g. Draw:3.50/3.60")
        rows.append((name.strip(), o, l, boost_calc(a.stake, o, l, comm, a.boost)))
    rows.sort(key=lambda r: -r[3]["profit"])
    head = f"£{a.stake:g} stake" + (f", {a.boost:g}% boost on the winnings part of the odds" if a.boost else " at the odds given")
    print(f"Boost / arb: {head}. Lay on {a.ex} ({comm:g}% commission). Best first:")
    print(f"  {'Selection':<22}{'Odds':>6}{'Boosted':>9}{'Lay':>7}{'Lay stake':>11}{'Liability':>11}{'Profit':>10}{'':>8}")
    for name, o, l, r in rows:
        flag = "" if r["profit"] > 0.005 else "  (no arb)"
        print(f"  {name[:21]:<22}{o:>6.2f}{r['boosted']:>9.2f}{l:>7.2f}{money(r['lay_stake']):>11}{money(r['liability']):>11}"
              f"{money(r['profit']):>10}{r['pct']:>7.1f}%{flag}")
    print("  The profit is the same whichever way the result goes. Place the bookie bet first, then lay straight away.")


def cmd_calc(a, conf):
    if a.type == "2up":
        if a.inplay is None:
            sys.exit("2up needs: STAKE BACK LAY_STAKE LAY_ODDS INPLAY_BACK_ODDS")
        r = twoup(a.stake, a.back, a.lay, a.extra, a.inplay)
        print(f"2UP paid out: £{a.stake:g} @ {a.back} (+{money(a.stake*(a.back-1))} from bookie). Open lay £{a.lay:g} @ {a.extra}.")
        print(f"  Lock in now: BACK the team on the exchange for {money(r['back_stake'])} @ {a.inplay}")
        print(f"     team wins {money(r['win'])}   /   team draws or loses {money(r['lose'])}")
        print(f"  Or let it ride: team wins {money(r['ride_win'])}   /   team draws or loses {money(r['ride_lose'])}")
        return
    comm = comm_for(conf, a.ex, a.comm)
    r = lay_calc(a.type, a.stake, a.back, a.lay, comm)
    label = {"q": "Qualifying bet", "snr": "Free bet (stake not returned)", "sr": "Free bet (stake returned)"}[a.type]
    print(f"{label}: £{a.stake:g} @ {a.back} — lay @ {a.lay} on {a.ex} ({comm:g}% commission)")
    print(f"  Lay stake   {money(r['lay_stake'])}")
    print(f"  Liability   {money(r['liability'])}   (needs to be in your {a.ex} balance)")
    print(f"  If bookie bet wins:   {money(r['back_wins'])}")
    print(f"  If exchange lay wins: {money(r['lay_wins'])}")
    if "retention_pct" in r:
        print(f"  Locked-in profit {money(r['profit'])}  = {r['retention_pct']:.1f}% of the free bet")
    else:
        print(f"  Qualifying loss {money(r['profit'])}")


def cmd_lay(a, conf):
    ev = [e for e in smk_events(a.days, a.fresh) if all(w in norm(e["name"]) for w in norm(a.team).split())]
    if not ev:
        print(f"No Smarkets football match matching '{a.team}' in the next {a.days} days.")
        return
    want = ("WINNER_3_WAY", "OVER_UNDER", "BTTS") if a.all else ("WINNER_3_WAY",)
    if a.cs:
        want += ("CORRECT_SCORE", "HALF_FULL")
    prices = smk_prices([e["id"] for e in ev[:8]], want)
    for e in ev[:8]:
        print(f"\n{e['name']}  —  {e['start'][:16].replace('T', ' ')} UTC")
        for mn, cs in prices.get(e["id"], {}).items():
            if a.all and mn.startswith("Over/under") and "2.5" not in mn:
                continue
            items = list(cs.values())
            if len(items) > 4:  # correct score / HT-FT: one line per selection, in a sensible order
                items.sort(key=lambda c: (c["lay"] is None, c["name"]))
                print(f"  {mn}:")
                for c in items:
                    print(f"      {c['name']:<22} lay {c['lay'] or '-':<7} (£{c['liq']:.0f} available)")
                continue
            row = "   ".join(f"{c['name']}: lay {c['lay'] or '-'} (£{c['liq']:.0f})" for c in items)
            print(f"  {mn:<20} {row}")


def cmd_match(a, conf):
    if a.type not in ("q", "snr", "sr"):
        sys.exit("--type must be q, snr or sr")
    bookies = [b.strip().lower() for b in a.bookies.split(",")] if a.bookies else []
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=a.days)
    games = []
    for sport in conf["sports"]:
        for g in odds_api(conf, sport, a.fresh):
            t = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
            if now < t < horizon:
                games.append(g)
    if not games:
        print("No games found (check sports list / key)."); return

    smk_ev = smk_events(a.days, a.fresh)
    link = {}
    for g in games:
        e = find_smk_event(smk_ev, g["home_team"], g["away_team"], g["commence_time"])
        if e:
            link[g["id"]] = e["id"]
    smk = smk_prices(list(set(link.values())))

    rows = []
    for g in games:
        # exchange lay prices for this game: {outcome_name: [(exchange, odds, liquidity)]}
        lays = {}
        for bm in g.get("bookmakers", []):
            if bm["key"] != "betfair_ex_uk":
                continue
            for m in bm["markets"]:
                if m["key"] == "h2h_lay":
                    for o in m["outcomes"]:
                        lays.setdefault(o["name"], []).append(("betfair", o["price"], None))
        sm = smk.get(link.get(g["id"]), {}).get("Full-time result", {})
        for ctype, c in sm.items():
            name = {"HOME": g["home_team"], "AWAY": g["away_team"], "DRAW": "Draw"}.get(ctype)
            if name and c["lay"]:
                lays.setdefault(name, []).append(("smarkets", c["lay"], c["liq"]))
        if not lays:
            continue
        for bm in g.get("bookmakers", []):
            if bm["key"].startswith("betfair_ex") or bm["key"] in ("smarkets", "matchbook"):
                continue
            if bookies and not any(b in bm["key"] or b in bm["title"].lower() for b in bookies):
                continue
            for m in bm["markets"]:
                if m["key"] != "h2h":
                    continue
                for o in m["outcomes"]:
                    back = o["price"]
                    if back < a.min_odds or (a.max_odds and back > a.max_odds):
                        continue
                    best = None
                    for ex, lay, liq in lays.get(o["name"], []):
                        try:
                            r = lay_calc(a.type, a.stake, back, lay, comm_for(conf, ex))
                        except ValueError:
                            continue
                        if liq is not None and liq < max(r["lay_stake"], conf["min_lay_liquidity"]) and not a.any_liq:
                            continue   # not enough money waiting at that lay price
                        if best is None or r["profit"] > best[3]["profit"]:
                            best = (ex, lay, liq, r)
                    if not best:
                        continue
                    ex, lay, liq, r = best
                    rows.append({"when": g["commence_time"], "game": f"{g['home_team']} v {g['away_team']}",
                                 "sel": o["name"], "bookie": bm["title"], "back": back, "ex": ex, "lay": lay,
                                 "liq": liq, "rating": 100 * back / lay, **r})
    rows.sort(key=lambda r: -r["profit"])
    if not rows:
        print("No matches found with those filters."); return
    head = "Free bet" if a.type != "q" else "Qual. loss"
    print(f"Top matches for a £{a.stake:g} {'qualifying bet' if a.type == 'q' else 'free bet (' + a.type.upper() + ')'}:")
    print(f"{'Kick-off (UK)':<14}{'Match':<36}{'Back':<16}{'Bookie':<15}{'Odds':>6}{'Lay':>7} {'Exch':<9}{'Liq':>6}{'Rating':>8}{'Lay stk':>9}{head:>11}")
    for r in rows[:a.top]:
        ko = datetime.fromisoformat(r["when"].replace("Z", "+00:00")).astimezone().strftime("%a %d %H:%M")
        liq = f"£{r['liq']:.0f}" if r["liq"] is not None else "-"
        res = money(r["profit"]) + (f" {r['retention_pct']:.0f}%" if "retention_pct" in r else "")
        print(f"{ko:<14}{r['game'][:35]:<36}{r['sel'][:15]:<16}{r['bookie'][:14]:<15}{r['back']:>6.2f}{r['lay']:>7.2f} "
              f"{r['ex']:<9}{liq:>6}{r['rating']:>7.1f}%{money(r['lay_stake']):>9}{res:>11}")
    q = cache_get("quota.json", 0) or (json.load(open(QUOTA)) if os.path.exists(QUOTA) else {})
    if q:
        print(f"\nOdds API credits left: {q.get('remaining')} (as of {q.get('at')}). Prices are cached "
              f"{conf['cache_minutes']} min; use --fresh to refresh. Always re-check odds before placing.")


def cmd_quota(a, conf):
    if os.path.exists(QUOTA):
        print(open(QUOTA).read())
    else:
        print("No Odds API calls made yet.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calc"); c.add_argument("type", choices=["q", "snr", "sr", "2up"])
    c.add_argument("stake", type=float); c.add_argument("back", type=float); c.add_argument("lay", type=float)
    c.add_argument("extra", type=float, nargs="?", help="2up: lay odds"); c.add_argument("inplay", type=float, nargs="?", help="2up: in-play back odds")
    c.add_argument("--ex", default="betfair"); c.add_argument("--comm", type=float)
    l = sub.add_parser("lay"); l.add_argument("team"); l.add_argument("--days", type=int, default=7)
    l.add_argument("--all", action="store_true", help="also over/under 2.5 and BTTS"); l.add_argument("--fresh", action="store_true")
    l.add_argument("--cs", action="store_true", help="also correct score and half-time/full-time (for laying bet builders)")
    m = sub.add_parser("match"); m.add_argument("--type", default="q"); m.add_argument("--stake", type=float, default=10)
    m.add_argument("--min-odds", type=float, default=1.5); m.add_argument("--max-odds", type=float, default=0)
    m.add_argument("--bookies", default=""); m.add_argument("--days", type=int, default=3)
    m.add_argument("--top", type=int, default=25); m.add_argument("--fresh", action="store_true")
    m.add_argument("--any-liq", action="store_true", help="include lays with too little money available")
    ac = sub.add_parser("acca", help="lay an acca one leg at a time")
    ac.add_argument("--type", choices=["q", "snr", "sr"], default="q"); ac.add_argument("--stake", type=float, default=10)
    ac.add_argument("--legs", required=True, help='BACK/LAY per leg in kick-off order, e.g. "2.20/2.30,1.40/1.42,1.67/1.69"')
    ac.add_argument("--won", type=int, default=0, help="legs already won")
    ac.add_argument("--sunk", type=float, help="liability already lost on the won legs' lays")
    ac.add_argument("--ex", default="smarkets"); ac.add_argument("--comm", type=float)
    bo = sub.add_parser("boost", help="arb a price boost; compare several selections")
    bo.add_argument("--stake", type=float, default=10, help="stake (use the boost's max stake)")
    bo.add_argument("--boost", type=float, default=0, help="%% added to the winnings part of the odds (0 if the odds are already boosted)")
    bo.add_argument("--sel", required=True, help='NAME:BOOKIE_ODDS/LAY_ODDS, comma separated, e.g. "Draw:3.50/3.60,Brentford:2.30/2.40"')
    bo.add_argument("--ex", default="smarkets"); bo.add_argument("--comm", type=float)
    sub.add_parser("quota")
    a = p.parse_args()
    conf = load_conf()
    {"calc": cmd_calc, "acca": cmd_acca, "boost": cmd_boost, "lay": cmd_lay, "match": cmd_match, "quota": cmd_quota}[a.cmd](a, conf)


if __name__ == "__main__":
    main()
