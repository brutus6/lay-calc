# Lay calc

A lay calculator, a 2UP trade-out calculator and an odds matcher. It comes in two forms:

- **Web page** (`index.html`): live at **https://brutus6.github.io/lay-calc/** (GitHub Pages, `main` branch, root). The page asks search engines not to index it.
  Everything runs in your browser. Your Odds API key and commission settings are saved in that browser only, never in this repo.
- **Command line** (`mb.py`): Python 3 + `requests`. This is what Claude runs.

Commission defaults: Betfair 5%, Smarkets 0%, Matchbook 0%. You can change them in the page's Settings or in the CLI config.
Only mainstream competitions are scanned (EPL, Championship, UCL, UEL, FA Cup, EFL Cup, La Liga, Serie A, Bundesliga, Ligue 1, SPL).

## Web page

| Tab | What it does |
|---|---|
| Lay calculator | Qualifying, free bet SNR (stake not returned) and SR (stake returned): lay stake, liability, profit/loss each way, retention %. |
| 2UP | Your team went 2 up and the bookie paid out. Shows the exchange back stake that locks in profit, against letting it ride. |
| Odds matcher | Ranks bookie back odds against exchange lay odds for your stake and bet type. Needs a free key from [the-odds-api.com](https://the-odds-api.com) (Settings tab). |

Exchange lay prices come from The Odds API (`h2h_lay`, Betfair/Smarkets/Matchbook). The page also tries live Smarkets prices with liquidity. If your browser blocks that, it says so and uses the Odds API prices only.
Each scan costs about 1 credit per competition per market: 22 credits with lay prices on. The free tier is 500 a month. Prices are cached for 30 minutes.

## Command line

```
mb calc q   10 2.88 3.1                 qualifying bet (stake, back odds, lay odds)
mb calc snr 30 3.75 3.85 --ex smarkets  free bet, stake not returned
mb calc sr  10 4.0  4.2                 free bet, stake returned
mb calc 2up 25 2.5 24.80 2.52 1.10      2UP: stake, back odds, lay stake, lay odds, in-play back odds
mb lay "arsenal" [--all]                live Smarkets lay prices + liquidity (no key needed)
mb match --type snr --stake 10 --bookies "william,paddy" --min-odds 3
mb quota                                Odds API credits left
```

Setup: `pip install requests`, then make `~/.config/oddsmatcher/config.json`:

```json
{ "odds_api_key": "YOUR_KEY", "commission": { "betfair": 5, "smarkets": 0 } }
```

That file holds your key. **Never commit it.** It lives outside the repo on purpose.

Always re-check the odds on the bookie and the exchange before placing a bet. Use round-number bookie stakes.
