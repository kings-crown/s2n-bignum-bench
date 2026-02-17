import csv
import json
import os
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
  if len(sys.argv) != 3:
    print("python3 collect-verdicts.py <eval dir> <output.csv>")
    exit(1)

  path = sys.argv[1]
  csvout_path = sys.argv[2]

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
