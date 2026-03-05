"""Collect top-level theorems into benchmark-ready problem definitions."""

"""
Stable IDs(with the .N suffixes for collisions) - with a growing project, 
the {linenum} part of the original ID is not stable, 
so we assign IDs based on the theorem name and file name.
The original 4-part ID is recorded in the "legacy_id" field
of each problem entry, so we can trace back to the original theorem if needed.
"""

import argparse
import json
import os
import sys
from typing import Sequence

verbose = False
quiet_mode = False

# A problem type is a dict
# key: a stable problem identifier of the form "{arch}.{filename}.{theorem_name}".
#   For the rare cases where the same theorem name appears more than once in the
#   same file, a 0-based occurrence index is appended: "{arch}.{filename}.{thm}.{N}".
# value: is another dict
# - "query": the HOL Light query
# - "category": the category
# - "json": the original JSON entry
# - "legacy_id": the original 4-part ID "{arch}.{filename}.{linenum}.{theorem_name}"

problems: dict[str, dict] = dict()

# Maps each base name to the list of problem keys assigned for it, for dupe detection.
_name_keys: dict[str, list[str]] = dict()

def adjust_line_col_nums(
    lines: Sequence[str], linenum_end: int, colnum_end: int
) -> tuple[int, int]:
  """Normalise 1-based line/column pairs so the column is in range for that line."""

  while True:
    assert linenum_end <= len(lines)
    current_line = lines[linenum_end - 1]
    if colnum_end > len(current_line):
      colnum_end -= len(current_line)
      linenum_end += 1
    else:
      break
  return linenum_end, colnum_end


def extract_string(
    lines: Sequence[str],
    linenum_start: int,
    colnum_start: int,
    linenum_end: int,
    colnum_end: int,
) -> str:
  """Return the substring bounded by the provided 1-based line/column markers."""

  linenum = linenum_start - 1
  colnum = colnum_start
  linenum_end, colnum_end = adjust_line_col_nums(lines, linenum_end, colnum_end)

  txt = ""
  while True:
    current_line = lines[linenum]
    if linenum + 1 == linenum_end:
      txt += current_line[colnum:colnum_end]
      break
    assert linenum < linenum_end
    txt += current_line[colnum:]
    colnum = 0
    linenum += 1
  return txt

def contains_anykw(keywords: Sequence[str], text: str) -> bool:
  """Return True when any keyword is present as a standalone token."""

  for keyword in keywords:
    if keyword.endswith("*"):
      token = f" {keyword[:-1]}"
      if token in text:
        return True
      continue

    token_prefixed = f" {keyword}"
    token_suffixed = f"{keyword} "
    if (
        token_prefixed in text
        or token_suffixed in text
        or f"({keyword}" in text
        or f"{keyword})" in text
    ):
      return True

  return False


def categorize(thm_name: str, goal: str, proof: str, toplevel_dir: str) -> tuple[bool, str]:
  """Classify a theorem, returning (drop?, classification or reason)."""

  # Normalise the goal so it starts (and ends) with the backtick-delimited term.
  # Some theorems don't necessarily start with the backtick-delimited term, e.g., they may start with "(`" or "(`\n", etc.
  # so the first character is '(' rather than '`'. So we count the backticks and extract the substring between the first and last backticks.
  goal = goal.strip()
  first_backtick = goal.find("`")
  last_backtick = goal.rfind("`")
  if first_backtick == -1 or last_backtick == -1 or first_backtick == last_backtick:
    return True, "the goal is evaluated at runtime"

  if first_backtick != 0 or last_backtick != len(goal) - 1:
    goal = goal[first_backtick : last_backtick + 1]
  if "_EQUIV" in thm_name or "SUBROUTINE_SAFE" in thm_name:
    return True, "not a proof related to functional correctness"
  if "SUBROUTINE_CORRECT" in thm_name:
    return True, "a top-level subroutine that just uses the _CORRECT of the core part"

  goal = goal.replace("\n", " ")
  goal = f" {goal} "

  if "_CORRECT" in thm_name and contains_anykw(["ensures"], goal):
    if toplevel_dir == "arm":
      return False, "functional_correctness_arm"
    if toplevel_dir == "x86":
      return False, "functional_correctness_x86"
    assert False, f"neither arm nor x86: {toplevel_dir}"

  if contains_anykw(
      [
          "read",
          "write",
          ":>",
          "nonoverlapping",
          "nonoverlapping_modulo",
          "contained",
          "contained_modulo",
      ],
      goal,
  ):
    return False, "program_state"

  if contains_anykw(["word_*", "word", "val"], goal):
    return False, "bit_vector"

  return False, "generic"


# path is, e.g., "/home/ubuntu/s2n-bignum-new/arm/proofs/bignum_add.ml"
# Return "arm"
def get_toplevel_dir(path: str) -> str:
  """Derive the architecture root (arm/x86/common) from a full file path."""

  prefix = "/s2n-bignum"
  idx = path.rfind(prefix)
  if idx == -1:
    raise ValueError(f"Path does not contain {prefix!r}: {path!r}")

  # Extract the path segment immediately after the s2n-bignum* directory.
  subpath = path[idx + 1 :]
  parts = subpath.split("/")
  if len(parts) < 2:
    raise ValueError(f"Cannot derive toplevel dir from path: {path!r}")

  return parts[1]


category_stats: dict[str, int] = dict()

def process_json(
    mlfile_path: str,
    json_data: Sequence[dict],
    json_data_nolinenum: Sequence[dict],
    output_cheat_ml_dir: str,
) -> None:
  """Populate the global problem table and emit CHEAT_TAC versions of proofs."""

  n = len(json_data_nolinenum)
  assert n == len(json_data)

  # Read the whole file.
  with open(mlfile_path, encoding="utf-8") as ml_file:
    ml_lines = list(ml_file.readlines())

  # Write a new .ml file with all proofs replaced with CHEAT_TAC!
  # The location is: If mlfile_path was "..../arm/bignum_add.ml", the location
  # becomes "{output_cheat_ml_dir}/arm/bignum_add.ml".
  # Also, record for each i'th top-level theorem how many lines have been
  # erased due to the replacements.
  line_shifts: list[int] = []
  parent_dir, mlfile = os.path.split(mlfile_path)
  _, arch = os.path.split(parent_dir)
  os.makedirs(os.path.join(output_cheat_ml_dir, arch), exist_ok=True)
  output_cheat_path = os.path.join(
      os.path.join(output_cheat_ml_dir, arch), mlfile)

  with open(output_cheat_path, "w", encoding="utf-8") as cheat_file:
    prev_line = 0
    prev_col = 0
    total_shift = 0

    for idx, item in enumerate(json_data_nolinenum):
      lst, cst = adjust_line_col_nums(
          ml_lines,
          int(item["proof_linenum_start"]),
          int(item["proof_colnum_start"]))
      snippet = extract_string(ml_lines, prev_line, prev_col, lst, cst)

      cheat_file.write(snippet)
      cheat_file.write("CHEAT_TAC")

      prev_line, prev_col = adjust_line_col_nums(
          ml_lines,
          int(item["proof_linenum_end"]),
          int(item["proof_colnum_end"]))

      line_shifts.append(total_shift)
      total_shift += prev_line - lst

    tail = extract_string(
        ml_lines, prev_line, prev_col, len(ml_lines), len(ml_lines[-1]))
    cheat_file.write(tail)

    if mlfile == "word_recip.ml" and not quiet_mode:
      print(line_shifts)

  # Now we have three .ml files:
  # 1. The original inlined file (input)
  #   - Path: mlfile_path
  # 2. The multiple files before inlining (located in s2n-bignum)
  #   - Paths: json_data[idx]["filename"]
  # 3. The inlined file with all proofs replaced with CHEAT_TAC
  #   - Path: output_cheat_path

  # Build the problem set, with the adjusted line numbers after CHEAT_TAC!
  for idx, itm in enumerate(json_data_nolinenum):
    thm_name = itm["theorem_name"]
    assert thm_name == json_data[idx]["theorem_name"]

    # The path of the source .ml file that includes this theorem, before inlining.
    file_fullpath = json_data[idx]["filename"]

    # Skip this theorem if the source .ml file was in the standalone hol-light
    # tree rather than the s2n-bignum repo. Use a trailing slash so we don't
    # get confused by parent directories like "s2n-bignum-bench".
    if "/hol-light" in file_fullpath and "/s2n-bignum/" not in file_fullpath:
      if verbose:
        print(f'{thm_name} is defined in HOL Light. skipping...')
      continue
    elif "/s2n-bignum/" not in file_fullpath:
      print(f'{thm_name} is neither in HOL Light nor s2n-bignum? file path: {file_fullpath}')
      exit(1)

    # Get the top-level directory name of this theorem; is it in common? x86? arm?
    toplevel_dir = get_toplevel_dir(file_fullpath)
    _, filename = os.path.split(file_fullpath)
    # strip ".ml"
    assert(filename.endswith(".ml")), filename
    filename = filename.removesuffix(".ml")

    # The stable identifier for this problem: {arch}.{filename}.{theorem_name}.
    # For collisions (same name in same file), an occurrence index is appended.
    toplevel_thm_linenum = json_data[idx]["toplevel_theorem_linenum_start"]
    legacy_id = f'{toplevel_dir}.{filename}.{toplevel_thm_linenum}.{thm_name}'
    base_name = f'{toplevel_dir}.{filename}.{thm_name}'

    # The goal.
    query = extract_string(ml_lines,
        int(itm["goal_linenum_start"]),
        int(itm["goal_colnum_start"]),
        int(itm["goal_linenum_end"]),
        int(itm["goal_colnum_end"]))

    # Check if this is a duplicate of an already-seen problem (same theorem
    # appearing in a different inlined file) by scanning all keys assigned to
    # this base_name for a JSON + query match.
    assigned_keys = _name_keys.get(base_name, [])
    duplicate_key = None
    for key in assigned_keys:
      if key in problems:
        entry = problems[key]
        if entry["json"] == json_data[idx] and entry["query"] == query:
          duplicate_key = key
          break

    if duplicate_key is not None:
      # Same theorem from a different inlined file — just add the location.
      line_shift = line_shifts[idx]
      linenum_in_cheat_ml = itm["toplevel_theorem_linenum_start"] - line_shift
      problems[duplicate_key]["inlined_locations"].append(
          (output_cheat_path, linenum_in_cheat_ml))
    else:
      # Assign a (possibly suffixed) problem name for a genuinely new theorem.
      n = len(assigned_keys)
      if n == 0:
        problem_name = base_name
        _name_keys[base_name] = [problem_name]
      elif n == 1:
        # Second distinct theorem: retroactively rename the first to .0
        first_key = assigned_keys[0]
        first_entry = problems.pop(first_key)
        new_first_key = f'{base_name}.0'
        problems[new_first_key] = first_entry
        assigned_keys[0] = new_first_key
        problem_name = f'{base_name}.1'
        assigned_keys.append(problem_name)
      else:
        problem_name = f'{base_name}.{n}'
        assigned_keys.append(problem_name)

      # This should not happen after collision-aware naming, but keep the
      # safety check for unexpected duplicates.
      if problem_name in problems:
        prev_entry = problems[problem_name]
        if prev_entry["json"] != json_data[idx]:
          print(f"{problem_name}: JSON information mismatch.")
          print(f'- Previous one: {prev_entry["json"]}')
          print(f'- New one: {json_data[idx]}')
          exit(1)
        elif prev_entry["query"] != query:
          print(f"{problem_name}: The query field mismatch.")
          print(f'- Previous one: {prev_entry["query"]}')
          print(f'- New one: {query}')
          exit(1)

        # (the inlined file path, the line num/column num, etc)
        line_shift = line_shifts[idx]
        linenum_in_cheat_ml = itm["toplevel_theorem_linenum_start"] - line_shift
        prev_entry["inlined_locations"].append(
            (output_cheat_path, linenum_in_cheat_ml))
      else:
        # For categorization. :)
        proof = extract_string(ml_lines,
            int(itm["proof_linenum_start"]),
            int(itm["proof_colnum_start"]),
            int(itm["proof_linenum_end"]),
            int(itm["proof_colnum_end"]))

        drop_this, category = categorize(thm_name, query, proof, toplevel_dir)

        if drop_this:
          if not quiet_mode:
            print(f"{problem_name}: Drop this theorem; why: {category}")

        else:
          if category in category_stats:
            category_stats[category] += 1
          else:
            category_stats[category] = 1

          if not quiet_mode:
            print(f"{problem_name}")
            print(f"- Query: {query}")
            print(f"- Category: {category}")

          line_shift = line_shifts[idx]
          linenum_in_cheat_ml = itm["toplevel_theorem_linenum_start"] - line_shift

          problems[problem_name] = {
              "json": json_data[idx],
              "category": category,
              "query": query,
              "legacy_id": legacy_id,
              "inlined_locations": [(output_cheat_path, linenum_in_cheat_ml)],
          }


if __name__ == '__main__':
  parser = argparse.ArgumentParser(
      prog='collect-problems.py',
      description="Collects the problems from top-level theorems dumped by 'make build_proofs' from s2n-bignum")
  parser.add_argument('--quiet', action='store_true', help='suppress per-problem logging')
  parser.add_argument('input_dir')
  parser.add_argument('output_json')
  parser.add_argument('output_ml_dir')
  args = parser.parse_args()

  quiet_mode = args.quiet

  output_cheat_ml_dir = args.output_ml_dir
  os.makedirs(output_cheat_ml_dir, exist_ok=True)

  for dirpath, _, filenames in sorted(os.walk(args.input_dir)):
    for filename in filenames:
      # X.nolinenum.json will be also consumed when reading X.json .
      if not filename.endswith('.json') or filename.endswith('.nolinenum.json'):
        continue

      json_nolinenum_path = os.path.join(
          dirpath, filename.removesuffix('.json') + '.nolinenum.json')
      if not os.path.exists(json_nolinenum_path):
        print(f"{json_nolinenum_path} does not exist")
        continue

      ml_nolinenum_path = os.path.join(
          dirpath, filename.removesuffix('.json') + '.ml')
      if not os.path.exists(ml_nolinenum_path):
        print(f"{ml_nolinenum_path} does not exist")
        continue

      if not quiet_mode:
        print(f"{dirpath}/{filename}")

      with open(os.path.join(dirpath, filename), encoding="utf-8") as f, \
          open(json_nolinenum_path, encoding="utf-8") as f_nolinenum:
        d = json.load(f)
        d_nolinenum = json.load(f_nolinenum)

      process_json(ml_nolinenum_path, d, d_nolinenum, output_cheat_ml_dir)

  if not quiet_mode:
    print(category_stats)
  with open(args.output_json, 'w', encoding="utf-8") as f:
    json.dump(problems, f, indent=2)
