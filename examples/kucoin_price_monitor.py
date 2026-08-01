"""Poll a KuCoin ticker and announce threshold crossings. Read-only.

Built to run unattended: transient network failures are retried with
backoff instead of ending the loop, so the process survives a screen
that switches off or a wifi radio that drops for a minute.

Run from the repository root, e.g.:

    python -m examples.kucoin_price_monitor --symbol BTC-USDT --above 120000 --below 100000

To keep it running after the terminal goes away, detach it and write to
a log file:

    nohup python -m examples.kucoin_price_monitor --log-file monitor.log &
"""

import argparse
import sys
import time
from datetime import datetime, timezone

from examples._env import load_env
from kucoin.client import KuCoinClient
from kucoin.data import last_price
from kucoin.resilient import retry_call


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTC-USDT", help="market symbol, e.g. BTC-USDT")
    parser.add_argument("--above", type=float, default=None, help="alert when price rises above this")
    parser.add_argument("--below", type=float, default=None, help="alert when price falls below this")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between polls")
    parser.add_argument(
        "--retries",
        type=int,
        default=8,
        help="attempts per poll before skipping it (backoff doubles, capped at 60s)",
    )
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=0.0,
        help="minimum seconds between routine price lines (0 prints every poll)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="append output here instead of stdout, for detached runs",
    )
    args = parser.parse_args()

    load_env()
    client = KuCoinClient()

    out = open(args.log_file, "a", buffering=1) if args.log_file else sys.stdout

    def emit(text: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] {text}", file=out, flush=True)

    def note_failure(attempt, exc, delay):
        emit(f"poll failed (attempt {attempt}): {exc} - retrying in {delay:g}s")

    emit(f"Monitoring {args.symbol} every {args.interval:g}s (Ctrl-C to stop)")
    was_above = None
    was_below = None
    last_report = 0.0
    offline = False

    try:
        while True:
            try:
                price = retry_call(
                    lambda: last_price(client, args.symbol),
                    attempts=max(args.retries, 1),
                    on_error=note_failure,
                )
            except Exception as exc:  # retry budget exhausted; keep the loop alive
                if not offline:
                    emit(f"giving up on this poll: {exc} - will keep trying")
                offline = True
                time.sleep(args.interval)
                continue

            if offline:
                emit("connection recovered")
                offline = False

            line = f"{args.symbol} {price}"
            crossed = False

            if args.above is not None:
                above = price > args.above
                if above and was_above is False:
                    line += f"  *** crossed ABOVE {args.above} ***"
                    crossed = True
                was_above = above
            if args.below is not None:
                below = price < args.below
                if below and was_below is False:
                    line += f"  *** crossed BELOW {args.below} ***"
                    crossed = True
                was_below = below

            now = time.monotonic()
            if crossed or args.heartbeat <= 0 or now - last_report >= args.heartbeat:
                emit(line)
                last_report = now

            time.sleep(args.interval)
    except KeyboardInterrupt:
        emit("stopped")
    finally:
        if out is not sys.stdout:
            out.close()


if __name__ == "__main__":
    main()
