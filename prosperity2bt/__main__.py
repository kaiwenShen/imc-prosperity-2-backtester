import sys
import webbrowser
from argparse import ArgumentParser
from collections import defaultdict
from datetime import datetime
from functools import partial, reduce
from http.server import HTTPServer, SimpleHTTPRequestHandler
from importlib import import_module, metadata, reload
from pathlib import Path

import numpy as np
from prosperity2bt.data import BacktestData, read_day_data
from prosperity2bt.file_reader import FileSystemReader, PackageResourcesReader
from prosperity2bt.models import BacktestResult
from prosperity2bt.runner import run_backtest
from typing import Any, Optional

def parse_algorithm(algorithm: str) -> Any:
    algorithm_path = Path(algorithm).expanduser().resolve()
    if not algorithm_path.is_file():
        raise ModuleNotFoundError(f"{algorithm_path} is not a file")

    sys.path.append(str(algorithm_path.parent))
    return import_module(algorithm_path.stem)

def parse_days(days: list[str], data_root: Optional[str]) -> list[BacktestData]:
    if data_root is not None:
        file_reader = FileSystemReader(Path(data_root).expanduser().resolve())
    else:
        file_reader = PackageResourcesReader()

    parsed_days = []

    if data_root is not None:
        data_root = Path(data_root).expanduser().resolve()

    for arg in days:
        if "-" in arg:
            round_num, day_num = map(int, arg.split("-", 1))

            day_data = read_day_data(file_reader, round_num, day_num)
            if day_data is None:
                print(f"Warning: no data found for round {round_num} day {day_num}")
                continue

            parsed_days.append(day_data)
        else:
            round_num = int(arg)

            parsed_days_in_round = []
            for day_num in range(-5, 6):
                day_data = read_day_data(file_reader, round_num, day_num)
                if day_data is not None:
                    parsed_days_in_round.append(day_data)

            if len(parsed_days_in_round) == 0:
                print(f"Warning: no data found for round {round_num}")
                continue

            parsed_days.extend(parsed_days_in_round)

    if len(parsed_days) == 0:
        print("Error: did not find data for any requested round/day")
        sys.exit(1)

    return parsed_days

def parse_out(out: Optional[str], no_out: bool) -> Optional[Path]:
    if out is not None:
        return Path(out).expanduser().resolve()

    if no_out:
        return None

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return Path.cwd() / "backtests" / f"{timestamp}.log"

def print_day_summary(result: BacktestResult) -> None:
    last_timestamp = result.activity_logs[-1].timestamp

    product_lines = []
    total_profit = 0

    for row in reversed(result.activity_logs):
        if row.timestamp != last_timestamp:
            break

        product = row.columns[2]
        profit = row.columns[-1]

        product_lines.append(f"{product}: {profit:,.0f}")
        total_profit += profit

    print(*reversed(product_lines), sep="\n")
    print(f"Total profit: {total_profit:,.0f}\n")

def merge_results(a: BacktestResult, b: BacktestResult, merge_profit_loss: bool) -> BacktestResult:
    sandbox_logs = a.sandbox_logs[:]
    activity_logs = a.activity_logs[:]
    trades = a.trades[:]

    a_last_timestamp = a.activity_logs[-1].timestamp
    timestamp_offset = a_last_timestamp + 100

    sandbox_logs.extend([row.with_offset(timestamp_offset) for row in b.sandbox_logs])
    trades.extend([row.with_offset(timestamp_offset) for row in b.trades])

    if merge_profit_loss:
        profit_loss_offsets = defaultdict(float)
        for row in reversed(a.activity_logs):
            if row.timestamp != a_last_timestamp:
                break

            profit_loss_offsets[row.columns[2]] = row.columns[-1]

        activity_logs.extend([
            row.with_offset(timestamp_offset, profit_loss_offsets[row.columns[2]])
            for row in b.activity_logs
        ])
    else:
        activity_logs.extend([row.with_offset(timestamp_offset, 0) for row in b.activity_logs])

    return BacktestResult(a.round_num, a.day_num, sandbox_logs, activity_logs, trades)

def write_output(output_file: Path, merged_results: BacktestResult) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w+", encoding="utf-8") as file:
        file.write("Sandbox logs:\n")
        file.write("\n".join(map(str, merged_results.sandbox_logs)))

        file.write("\n\n\n\nActivities log:\n")
        file.write("day;timestamp;product;bid_price_1;bid_volume_1;bid_price_2;bid_volume_2;bid_price_3;bid_volume_3;ask_price_1;ask_volume_1;ask_price_2;ask_volume_2;ask_price_3;ask_volume_3;mid_price;profit_and_loss\n")
        file.write("\n".join(map(str, merged_results.activity_logs)))

        file.write("\n\n\n\n\nTrade History:\n")
        file.write("[\n")
        file.write(",\n".join(map(str, merged_results.trades)))
        file.write("]")

def print_overall_summary(results: list[BacktestResult]) -> None:
    print(f"Profit summary:")

    total_profit = 0
    total_profit_all = []
    for result in results:
        last_timestamp = result.activity_logs[-1].timestamp

        profit = 0
        profit_all = []
        for row in reversed(result.activity_logs):
            if row.timestamp != last_timestamp:
                break

            profit += row.columns[-1]
            profit_all.append(row.columns[-1])
        print(f"Round {result.round_num} day {result.day_num}: {profit:,.0f}, Sharpe:{profit/np.std(profit_all):,.2f}, "
              f"Zen score:{(profit/np.std(profit_all))**0.6*profit**0.4:,.2f}")
        total_profit += profit
        total_profit_all.extend(profit_all)

    print(f"Total profit: {total_profit:,.0f}, Sharpe:{total_profit/np.std(total_profit_all):,.2f}, "
          f"Zen score:{(total_profit/np.std(total_profit_all))**0.6*total_profit**0.4:,.2f}\n")

class HTTPRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        return super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return

def open_visualizer(output_file: Path, no_requests: int) -> None:
    http_handler = partial(HTTPRequestHandler, directory=output_file.parent)
    http_server = HTTPServer(("localhost", 0), http_handler)

    webbrowser.open(f"https://jmerle.github.io/imc-prosperity-2-visualizer/?open=http://localhost:{http_server.server_port}/{output_file.name}")

    # Chrome makes 2 requests: 1 OPTIONS request to check for CORS headers and 1 GET request to get the data
    # Some users reported their browser only makes 1 request, which is covered by the --vis-requests option
    for _ in range(no_requests):
        http_server.handle_request()

def format_path(path: Path) -> str:
    cwd = Path.cwd()
    if path.is_relative_to(cwd):
        return str(path.relative_to(cwd))
    else:
        return str(path)

def main() -> None:
    parser = ArgumentParser(prog="prosperity2bt", description="Run a backtest.")
    parser.add_argument("algorithm", type=str, help="path to the Python file containing the algoritm to backtest")
    parser.add_argument("days", type=str, nargs="+", help="the days to backtest on (<round>-<day> for a single day, <round> for all days in a round)")
    parser.add_argument("--merge-pnl", action="store_true", help="merge profit and loss across days")
    parser.add_argument("--vis", action="store_true", help="open backtest result in visualizer when done")
    parser.add_argument("--out", type=str, help="path to save output log to (defaults to backtests/<timestamp>.log)")
    parser.add_argument("--data", type=str, help="path to data directory (must look similar in structure to https://github.com/jmerle/imc-prosperity-2-backtester/tree/master/prosperity2bt/resources)")
    parser.add_argument("--print", action="store_true", help="print the trader's output to stdout while it's running")
    parser.add_argument("--no-trades-matching", action="store_true", help="disable matching orders against market trades")
    parser.add_argument("--no-out", action="store_true", help="skip saving the output log to a file")
    parser.add_argument("--no-progress", action="store_true", help="don't show progress bars")
    parser.add_argument("--vis-requests", type=int, default=2, help="number of requests the visualizer is expected to make to the backtester's HTTP server when using --vis")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {metadata.version(__package__)}")

    args = parser.parse_args()

    if args.vis and args.no_out:
        print("Error: --vis and --no-out are mutually exclusive")
        sys.exit(1)

    if args.out is not None and args.no_out:
        print("Error: --out and --no-out are mutually exclusive")
        sys.exit(1)

    try:
        trader_module = parse_algorithm(args.algorithm)
    except ModuleNotFoundError as e:
        print(f"{args.algorithm} is not a valid algorithm file: {e}")
        sys.exit(1)

    if not hasattr(trader_module, "Trader"):
        print(f"{args.algorithm} does not expose a Trader class")
        sys.exit(1)

    days = parse_days(args.days, args.data)
    output_file = parse_out(args.out, args.no_out)

    results = []
    for day in days:
        print(f"Backtesting {args.algorithm} on round {day.round_num} day {day.day_num}")

        reload(trader_module)
        trader = trader_module.Trader()

        result = run_backtest(trader, day, args.print, args.no_trades_matching, args.no_progress)
        print_day_summary(result)

        results.append(result)

    merged_results = reduce(lambda a, b: merge_results(a, b, args.merge_pnl), results)

    if len(days) > 1:
        print_overall_summary(results)

    if output_file is not None:
        write_output(output_file, merged_results)
        print(f"\nSuccessfully saved backtest results to {format_path(output_file)}")

    if args.vis:
        open_visualizer(output_file, args.vis_requests)

if __name__ == "__main__":
    main()
