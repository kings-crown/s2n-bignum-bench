import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

problems = dict()

timeouts = dict()

# Category -> problems
answered_problems = dict()

answers = dict()
results = dict()

TEMPLATE_CACHE_VERSION = 1
TEMPLATE_CACHE_PATH = Path(__file__).parent / 'template-cache.json'
template_cache_data = {'version': TEMPLATE_CACHE_VERSION, 'entries': {}}
template_cache_modified = False

bench_run_code = \
"""
exception S2n_bignum_bench_timeout;;

let s2n_bignum_bench_timed_fun f x timeout =
  let _ =
    Sys.set_signal Sys.sigalrm (Sys.Signal_handle
			(fun _ -> raise S2n_bignum_bench_timeout))
  in
  ignore (Unix.alarm timeout);
  try
    let r = f x in
    ignore (Unix.alarm 0); r
  with
  | e  -> ignore (Unix.alarm 0); raise e

let bench_run (problem_name:string) (query:term) (tac0:unit->tactic)
              (output_txt_path:string) (timeout:int) =
  let oc = open_out output_txt_path in
  let axioms_before = axioms() in
  (try
    let tac:tactic = tac0 () in
    let _:thm = s2n_bignum_bench_timed_fun prove (query, tac) timeout in
    let axioms_after = axioms() in
    output_string oc (
      if axioms_before = axioms_after then "OK" else "CHEATING")
  with Failure _ -> output_string oc "FAIL"
  | S2n_bignum_bench_timeout -> output_string oc "TIMEOUT"
  | _ -> output_string oc "ERROR");
  close_out oc;;

"""


def load_template_cache():
  global template_cache_data
  if TEMPLATE_CACHE_PATH.exists():
    try:
      with open(TEMPLATE_CACHE_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
      if isinstance(data, dict) and data.get('version') == TEMPLATE_CACHE_VERSION:
        template_cache_data = data
    except (OSError, json.JSONDecodeError):
      template_cache_data = {'version': TEMPLATE_CACHE_VERSION, 'entries': {}}


def save_template_cache():
  global template_cache_modified
  if not template_cache_modified:
    return
  with open(TEMPLATE_CACHE_PATH, 'w', encoding='utf-8') as f:
    json.dump(template_cache_data, f, indent=2)
  template_cache_modified = False


def build_template_cache_key(problems_dict, answered_order):
  problems_blob = json.dumps(problems_dict, sort_keys=True, separators=(',', ':')).encode()
  problems_hash = hashlib.sha256(problems_blob).hexdigest()
  answered_blob = '\n'.join(answered_order).encode()
  answered_hash = hashlib.sha256(answered_blob).hexdigest()
  return f"{problems_hash}:{answered_hash}"


def deserialize_cached_templates(cache_entry):
  templates = dict()
  for tpl_path, entries in cache_entry.items():
    templates[tpl_path] = [(item[0], item[1]) for item in entries]
  return templates


def serialize_templates_for_cache(templates):
  serialized = dict()
  for tpl_path, entries in templates.items():
    serialized[tpl_path] = [[item[0], item[1]] for item in entries]
  return serialized


def get_cached_templates(cache_key):
  return template_cache_data.get('entries', {}).get(cache_key)


def store_templates(cache_key, templates):
  global template_cache_modified
  if 'entries' not in template_cache_data:
    template_cache_data['entries'] = dict()
  template_cache_data['entries'][cache_key] = serialize_templates_for_cache(templates)
  template_cache_modified = True


def write_query_and_answer(f, problem_name, judge_output_path):
  query = problems[problem_name]["query"]
  category = problems[problem_name]["category"]
  answer = answers[problem_name]

  assert category in timeouts, f"{category} not in timeouts.json"

  # Wrap the query so higher-order expressions stay a single bench_run arg
  f.write(
      f'bench_run "{problem_name}" ({query}) (fun () -> {answer}) '
      f'"{judge_output_path}" {timeouts[category]};;\n\n'
  )


evaldir = None


# Return True if the expression could not be compiled.
def check_parsing_error(prob_id):
  template_file_info = problems[prob_id]["inlined_locations"][0]
  file_path, linenum = template_file_info[0], template_file_info[1]

  synchk_path = os.path.join(evaldir, prob_id + ".synchk.ml")

  with open(file_path, "r", encoding='utf-8') as source, \
      open(synchk_path, "w", encoding='utf-8') as target:
    lines = list(source.readlines())
    for line in lines[:linenum]:
      target.write(line)
    # Force the candidate tactic to type-check in the same shape expected by
    # bench_run (unit -> tactic), so mis-shaped answers fail here.
    # removed this: target.write(f"let it = {answers[prob_id]};;\n")
    target.write(f"let _ : unit -> tactic = (fun () -> {answers[prob_id]});;\n")

  result = subprocess.run(['bash', 'synchk.sh', synchk_path])
  exit_code = result.returncode
  os.remove(synchk_path)

  if exit_code != 0:
    print(f"{prob_id}: syntactic check FAILED")
  else:
    print(f"{prob_id}: syntactic check PASSED")
  return prob_id, exit_code != 0


def build_templates(answ_problems_list):
  if not answ_problems_list:
    return dict()

  cache_key = build_template_cache_key(problems, answ_problems_list)
  cached_entry = get_cached_templates(cache_key)
  if cached_entry is not None:
    return deserialize_cached_templates(cached_entry)

  templates = dict()
  for problem_name in answ_problems_list:
    template_candidates = problems[problem_name]["inlined_locations"]

    found = False
    for candidate in template_candidates:
      filename = candidate[0]
      if filename in templates:
        templates[filename].append((problem_name, candidate[1]))
        found = True
        break

    if not found and template_candidates:
      filename, linenum = template_candidates[0]
      templates.setdefault(filename, []).append((problem_name, linenum))

  for tpl in templates:
    templates[tpl].sort(key=lambda item: item[1])

  if templates:
    store_templates(cache_key, templates)

  return templates


def generate_grader(num_cores):
  global evaldir

  timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
  evaldir = f"eval-{timestamp}"
  os.makedirs(evaldir, exist_ok=True)

  with open(f"log-eval-{timestamp}.txt", "w", encoding='utf-8') as logf:
    submitted_problems = sorted(answers.keys())

    parsing_results = []
    if submitted_problems:
      if num_cores <= 1:
        parsing_results = [check_parsing_error(prob_id) for prob_id in submitted_problems]
      else:
        with multiprocessing.Pool(processes=num_cores) as pool:
          parsing_results = pool.map(check_parsing_error, submitted_problems)

    for prob_id, failed in parsing_results:
      if failed:
        logf.write(f"{prob_id}: compile error\n")
        answers.pop(prob_id, None)
        category = problems[prob_id]["category"]
        if category in answered_problems and prob_id in answered_problems[category]:
          answered_problems[category].remove(prob_id)
          if not answered_problems[category]:
            del answered_problems[category]

    answ_problems_list = []
    for cat in ['functional_correctness_arm', 'functional_correctness_x86',
                'program_state', 'bit_vector', 'generic']:
      if cat in answered_problems and answered_problems[cat]:
        answ_problems_list.extend(sorted(answered_problems[cat]))

    templates = build_templates(answ_problems_list)

    logf.write("\n<Portfolio>\n")
    for template_path, entries in templates.items():
      assert os.path.exists(template_path), f"template file does not exist: {template_path}"
      with open(template_path, "r", encoding='utf-8') as template_file:
        template_lines = list(template_file.readlines())

      output_filename = template_path.replace("/", "_")
      output_path = os.path.join(evaldir, output_filename)

      logf.write(f"{output_path} includes:\n")

      prevline = 0
      with open(output_path, "w", encoding='utf-8') as out_file:
        for index, (problem_name, linenum) in enumerate(entries):
          for line in template_lines[prevline:linenum]:
            out_file.write(line)

          if index == 0:
            out_file.write(bench_run_code)

          judge_output_path = os.path.join("..", evaldir, f"{problem_name}.judge.txt")
          write_query_and_answer(out_file, problem_name, judge_output_path)
          logf.write(f"  {problem_name}\n")

          prevline = linenum

  print(evaldir)


if __name__ == '__main__':
  curdir = Path(__file__).parent.resolve()
  problems_json_path = os.path.join(curdir, "problems.json")
  with open(problems_json_path, "r", encoding='utf-8') as f:
    problems = json.load(f)

  timeouts_json_path = os.path.join(curdir, "timeouts.json")
  with open(timeouts_json_path, "r", encoding='utf-8') as f:
    timeouts = json.load(f)

  load_template_cache()

  if len(sys.argv) != 3:
    print("python3 combine-answer-and-setup.py <dir> <num cores>")
    exit(1)

  topdir = sys.argv[1]
  num_cores = int(sys.argv[2])

  for prob_id in os.listdir(topdir):
    if prob_id not in problems:
      print(f"subdirectory {prob_id} in {topdir} is not a valid problem name")
      exit(1)

    answ_path = os.path.join(topdir, prob_id, "answer.txt")
    if not os.path.exists(answ_path):
      print(f"{answ_path} does not exist")
      results[prob_id] = "-"
      continue

    category = problems[prob_id]["category"]
    answered_problems.setdefault(category, []).append(prob_id)

    with open(answ_path, "r", encoding='utf-8') as answer_file:
      answers[prob_id] = "".join(list(answer_file.readlines()))

  generate_grader(num_cores)
  save_template_cache()
