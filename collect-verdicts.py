import argparse
import csv
import json
import os
import shutil
import sys
from pathlib import Path

problems = dict()
verdicts = dict()

possible_verdicts = [
  "OK", "FAIL", "CHEATING", "ERROR", "TIMEOUT"
]

def collect_verdicts(evaldir_path):
  for fpath in os.listdir(evaldir_path):
    if not fpath.endswith(".judge.txt"):
      continue

    fpath = os.path.join(evaldir_path, fpath)
    probname = os.path.basename(fpath.removesuffix(".judge.txt"))
    if probname not in problems:
      print(f"Illegal judge.txt: does not correspond to any problem: {probname}")
      exit(1)

    with open(fpath, "r") as f:
      ls = list(f.readlines())
      if len(ls) > 1:
        print(f"Cannot understand the file contents of {fpath}")
        exit(1)
      elif len(ls) == 0:
        print(f"Empty file contents {fpath}")
        continue
      verdict = ls[0].strip()
      if verdict not in possible_verdicts:
        print(f"{fpath}: uninterpretable verdict: {verdict}")
      verdicts[probname] = verdict

if __name__ == '__main__':
  parser = argparse.ArgumentParser(
      description="Collect judge verdicts from an eval directory into a CSV.")
  parser.add_argument("evaldir", help="Path to eval-<timestamp> directory")
  parser.add_argument("output_csv", help="Output CSV path")
  parser.add_argument("--clean", action="store_true",
      help="Remove the eval directory (and its log-eval-*.txt) after writing the CSV")
  args = parser.parse_args()

  path = args.evaldir
  csvout_path = args.output_csv

  curdir = Path(__file__).parent.resolve()
  problems_json_path = os.path.join(curdir, "problems.json")
  with open(problems_json_path, "r") as f:
    problems = json.load(f)

  collect_verdicts(path)

  with open(csvout_path, "w") as f:
    w = csv.writer(f, delimiter=',')
    problems = list(verdicts.keys())
    problems.sort()

    w.writerow(['Problem', 'Verdict'])
    verdicts_count = dict()

    for p in problems:
      w.writerow([p, str(verdicts[p])])

      if verdicts[p] in verdicts_count:
        verdicts_count[verdicts[p]] += 1
      else:
        verdicts_count[verdicts[p]] = 1

    w.writerow(['--',''])
    w.writerow(['SUMMARY',''])
    ks = sorted(list(verdicts_count.keys()))
    for k in ks:
      w.writerow([k, str(verdicts_count[k])])

  if args.clean:
    eval_path = Path(path).resolve()
    # Remove corresponding log-eval-*.txt (matches the timestamp in the dir name)
    eval_name = eval_path.name  # e.g. "eval-20250305-123456"
    timestamp = eval_name.removeprefix("eval-")
    log_file = eval_path.parent / f"log-{eval_name}.txt"
    if log_file.exists():
      log_file.unlink()
      print(f"Removed {log_file}")
    shutil.rmtree(eval_path)
    print(f"Removed {eval_path}")
