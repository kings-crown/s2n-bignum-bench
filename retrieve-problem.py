"""Utilities for listing and retrieving s2n-bignum benchmark problems.
Usage:
  python retrieve-problem.py list [--category <category>]
  python retrieve-problem.py retrieve --outputdir <path> [--name <problem_id> | --category <category>] [--csv <path>] [--csv-only]

  Examples:
  python3 retrieve-problem.py retrieve --outputdir problems 
  python3 retrieve-problem.py retrieve --outputdir problems --csv problems.csv --csv-only
"""

import argparse
import csv
import json
import os
from pathlib import Path
from typing import Dict, Iterable, List

CATEGORIES = [
    'functional_correctness_arm',
    'functional_correctness_x86',
    'program_state',
    'bit_vector',
    'generic',
]

PROBLEMS: Dict[str, dict] = {}


def handle_retrieve(args) -> None:
  """Handle the `retrieve` subcommand by materialising the requested problems."""

  def print_problem(prob_id: str) -> None:
    problem_dir = os.path.join(args.outputdir, prob_id)
    os.makedirs(problem_dir, exist_ok=True)

    # Prepare setup code
    org_file, linenum = PROBLEMS[prob_id]["inlined_locations"][0]
    with (
        open(os.path.join(problem_dir, "setup.ml"), "w", encoding="utf-8") as setupf,
        open(org_file, "r", encoding="utf-8") as reference_file,
    ):
      for idx, line in enumerate(reference_file.readlines(), start=1):
        setupf.write(line)
        if idx >= linenum:
          break

    # Write the query
    with open(os.path.join(problem_dir, "query.txt"), "w", encoding="utf-8") as query_file:
      query_file.write(PROBLEMS[prob_id]["query"])

  selected: List[str] = []

  if args.name:
    if args.name not in PROBLEMS:
      print(f"Cannot find problem identifier: {args.name}")
      exit(1)
    selected = [args.name]
  elif args.category:
    selected = [pid for pid, data in PROBLEMS.items() if data["category"] == args.category]
  else:
    selected = list(PROBLEMS.keys())

  if args.csv_only:
    if not args.csv:
      print("--csv path is required when --csv-only is set")
      exit(1)
    write_csv(selected, args.csv)
    return

  for prob_id in selected:
    print_problem(prob_id)

  if args.csv:
    write_csv(selected, args.csv)


def write_csv(problem_ids: Iterable[str], csv_path: str) -> None:
  with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
    writer = csv.writer(csvfile)
    writer.writerow(["problem_id", "category", "query"])
    for prob_id in problem_ids:
      data = PROBLEMS[prob_id]
      writer.writerow([prob_id, data["category"], data["query"]])


def handle_list(args) -> None:
  """Handle the `list` subcommand by printing matching problem identifiers."""

  for prob_id in sorted(PROBLEMS):
    if args.category and PROBLEMS[prob_id]["category"] != args.category:
      continue
    print(prob_id)


def main() -> None:
  curdir = Path(__file__).parent.resolve()
  problems_json_path = os.path.join(curdir, "problems.json")
  with open(problems_json_path, "r", encoding="utf-8") as f:
    global PROBLEMS
    PROBLEMS = json.load(f)

  parser = argparse.ArgumentParser(
      prog='retrieve-problem.py',
      description='Retrieve problem(s) of s2n-bignum-bench')

  subparsers = parser.add_subparsers(
      dest='command',
      help='Available commands',
      required=True)

  # Parser for the "retrieve" command
  retrieve_parser = subparsers.add_parser(
      'retrieve',
      help='Retrieve problems with optional filters'
  )
  retrieve_parser.add_argument(
      '--category',
      choices=CATEGORIES,
      help='Filter by problem category')
  retrieve_parser.add_argument(
      '--name',
      type=str,
      help='Filter by the problem name')
  retrieve_parser.add_argument(
      '--outputdir',
      type=str,
      help='Specify output directory path',
      required=True)
  retrieve_parser.add_argument(
      '--csv',
      type=str,
      help='Optional path to write a CSV summary of the selected problems')
  retrieve_parser.add_argument(
      '--csv-only',
      action='store_true',
      help='Emit the CSV only, without materialising the problem directories')
  retrieve_parser.set_defaults(func=handle_retrieve)

  # Parser for the "list" command
  list_parser = subparsers.add_parser(
      'list',
      help='List all items')
  list_parser.add_argument(
      '--category',
      choices=CATEGORIES,
      help='Filter by problem category')
  list_parser.set_defaults(func=handle_list)

  args = parser.parse_args()
  args.func(args)


if __name__ == '__main__':
  main()
