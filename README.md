# finance-data-collector

Downloads NSE option chain snapshots from <https://www.nseindia.in/option-chain> on a
schedule and stores each one as an LZMA-compressed CSV.

## What it collects

- **Indices:** NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50
- **Stocks:** every F&O stock NSE lists (ABB, ABCAPITAL, ...), fetched fresh each cycle

For each symbol it saves the **nearest expiry**, which is what the site shows by default. The
CSV columns match the site's "Download (.csv)" button:

```
CALLS OI, CALLS CHNG IN OI, CALLS VOLUME, CALLS IV, CALLS LTP, CALLS CHNG, CALLS BID QTY,
CALLS BID, CALLS ASK, CALLS ASK QTY, STRIKE, PUTS BID QTY, PUTS BID, PUTS ASK, PUTS ASK QTY,
PUTS CHNG, PUTS LTP, PUTS IV, PUTS VOLUME, PUTS CHNG IN OI, PUTS OI
```

## Folder layout

All times are IST:

```
data/
  NIFTY/
    2026-11-18/
      2026-11-18_12-15-12.csv.xz
      2026-11-18_12-20-12.csv.xz
  ABB/
    2026-11-18/
      2026-11-18_12-15-21.csv.xz
```

If NSE's data timestamp hasn't changed since the last save for a symbol, that snapshot
is skipped so you don't get duplicate files.

## Usage

```bash
uv sync

# Scheduler: one cycle every 5 minutes, Mon-Fri 09:15-15:30 IST only
uv run finance-data-collector

# One cycle, then exit (good for cron)
uv run finance-data-collector --once

# Only some symbols, every 60 seconds
uv run finance-data-collector -s NIFTY -s BANKNIFTY -s ABB --interval 60

# All options
uv run finance-data-collector --help
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--out` | `data` | Output folder |
| `--symbol`, `-s` | all | Only this symbol; repeat for more |
| `--interval` | `300` | Seconds between cycle starts. If a cycle runs longer than this, the next one starts right after it. |
| `--delay` | `1.0` | Pause between requests, so NSE doesn't block you |
| `--once` | off | Run a single cycle and exit (exit code 1 if every symbol failed) |
| `--ignore-market-hours` | off | Also run outside market hours |
| `--log-dir` | `logs` | Folder for the log file |
| `--verbose`, `-v` | off | Show debug messages on screen too |

While it runs, the terminal shows a progress bar for each cycle and a summary table
(saved / unchanged / skipped / failed) when the cycle ends. Stop it with Ctrl+C or `kill`.

## Log file

Everything is also written to `logs/collector.log` so you can check a run afterwards:

- the settings each run started with, and when it stopped
- one line per saved snapshot: symbol, expiry, strike count, NSE's data time and file path
- unchanged / skipped symbols, each retry, and each failure with its error
- a summary line per cycle, naming any failed symbols

The file rotates at midnight (`collector.log.2026-11-18`, ...), and the last 30 days are kept.
To find problems quickly:

```bash
grep -E "WARNING|ERROR" logs/collector.log
```

## Reading the files

```python
import pandas as pd
df = pd.read_csv("data/NIFTY/2026-11-18/2026-11-18_12-15-12.csv.xz")  # pandas decompresses .xz itself
```

From the shell: `xz -dc file.csv.xz | head`.

## Notes

- NSE moved from nseindia.com to nseindia.in and resets HTTP/2 connections, so the client
  uses HTTP/1.1 and loads the option chain page first to get session cookies. It refreshes
  them automatically when they expire.
- Exchange holidays are not built in. On a holiday NSE's timestamp doesn't move, so the
  skip-unchanged check means nothing new gets written.
