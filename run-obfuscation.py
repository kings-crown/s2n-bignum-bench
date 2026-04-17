import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

problems = dict()

obfuscator_code = \
"""
let s2n_bignum_bench_remove_invented_types: term -> term =
  let get_invented_types: (term -> hol_type list) = 
    let rec find_invented_tys (ty:hol_type): hol_type list =
      match ty with
      | Tyapp(a,tyargs) -> List.concat_map find_invented_tys tyargs
      | Tyvar s -> if String.starts_with ~prefix:"?" s then [ty] else []
    in
    let rec fn t =
      match t with
      | Var (_,ty) -> find_invented_tys ty
      | Const (_,ty) -> find_invented_tys ty
      | Comb (t1,t2) -> (fn t1) @ (fn t2)
      | Abs (t1,t2) -> (fn t1) @ (fn t2) in
    fun t ->
      let res = List.sort compare (fn t) in
      uniq res
  in
  
  fun t ->
    let invtys = get_invented_types t in
    let newvar_id = ref 0 in
    itlist (fun invty t ->
        let newty = mk_vartype ("bench_arbty" ^ (string_of_int !newvar_id)) in
        let _ = newvar_id := !newvar_id + 1 in
        inst [newty, invty] t)
      invtys t;;
      
let s2n_bignum_bench_print_obfuscated_query (t:term) (output_txt_path:string) =
  let t' = s2n_bignum_bench_remove_invented_types t in
  let orgval = !print_types_of_subterms in
  let _ = print_types_of_subterms := 2 in
  let str_t' = string_of_term t' in
  (try
    let oc = open_out output_txt_path in
    (try
      let t'' = parse_term str_t' in
      if t'' <> t' then
       (output_string oc "FAIL: parsing roundtrip: ";
        output_string oc str_t')
      else
       (output_string oc "`";
        output_string oc str_t';
        output_string oc "`")
    with Failure s ->
     (output_string oc "FAIL: parsing failed: ";
      output_string oc s;
      output_string oc ("\n" ^ str_t'))
    | Hol_lib.Noparse ->
     (output_string oc "FAIL: Hol_lib.Noparse";
      output_string oc ("\n" ^ str_t')));
    close_out oc
  with _ -> failwith ("Could not store to " ^ output_txt_path));
  print_types_of_subterms := orgval;;
"""

def write_query(f, problem_name, output_path):
  query = problems[problem_name]["query"]

  # Wrap query in parentheses so function-application queries remain a single argument.
  # Some theorems don't necessarily start with the backtick-delimited, helps resove parsing issues.
  f.write(f's2n_bignum_bench_print_obfuscated_query ({query}) "{output_path}";;\n\n')

def obfuscate(num_cores):
  timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
  compiledir = f"obfus-compile-{timestamp}"
  rundir = f"obfus-run-{timestamp}"
  os.makedirs(compiledir)
  os.makedirs(rundir)

  templates = dict()

  for prob_id in problems:
    file_path, linenum = problems[prob_id]["inlined_locations"][0]
    if file_path not in templates:
      templates[file_path] = [(prob_id, linenum)]
    else:
      templates[file_path].append((prob_id, linenum))

  print(f"Writing obfuscated query printer to {compiledir}")

  for t in templates:
    templates[t].sort(key=lambda x:x[1])

    with open(t, "r") as f:
      lines = list(f.readlines())

    with open(os.path.join(compiledir, t.replace("/", "_")), "w") as wf:
      prevline = 0
      for i in range(0, len(templates[t])):
        prob_id, linenum = templates[t][i]
        for j in range(prevline, linenum):
          wf.write(lines[j])

        if i == 0:
          wf.write(obfuscator_code)
        write_query(wf, prob_id, os.path.join("..", os.path.join(rundir, prob_id + ".obfus.txt")))
        prevline = linenum

  # Now compile & run them.
  # If the native compiler is used, the assembler raises a weird error message about the distance
  # between conditional jumps and labels. So, let's use run-answers-bytecode.sh .
  print("Compiling the source codes and extracting obfuscated queries...")
  result = subprocess.run(['bash', 'run-answers-bytecode.sh', compiledir, str(num_cores)])
  print(f"Stored at {rundir}")
  exit_code = result.returncode
  if exit_code != 0:
    print("run-answers-bytecode.sh did not exit successfully")
    exit(1)

  return rundir


if __name__ == '__main__':
  if len(sys.argv) != 4:
    print("python3 run-obfuscation.py <problems.json (input)> <num cores> <problems.json (output)>")
    exit(1)

  with open(sys.argv[1], "r") as f:
    problems = json.load(f)

  num_cores = int(sys.argv[2])

  # Run obfuscation. If the resulting directory already exists from previous run,
  # you can omit this invocation.
  obfus_run_dir = obfuscate(num_cores)

  # check obfuscation results
  obfus_problems = []
  for f in os.listdir(obfus_run_dir):
    obfus_problems.append(f.removesuffix(".obfus.txt"))

  has_unobfus_res = False
  for p in problems:
    if p not in obfus_problems:
      print(f"Error: Problem {p} not obfuscated!!")
      locations = problems[p].get("inlined_locations", [])
      fail_source = locations[0][0] if locations else "unknown template"
      print(f"There was a problem in building or running {fail_source}")
      has_unobfus_res = True

  if has_unobfus_res:
    exit(1)

  # Update problem
  obfus_cnt = 0
  for fname in os.listdir(obfus_run_dir):
    probname = fname.removesuffix(".obfus.txt")
    with open(os.path.join(obfus_run_dir, fname), "r") as f:
      ls = list(f.readlines())
      if "FAIL" in ls[0]:
        print(f"Note: obfuscation of {probname} failed")
        continue

      obfus_cnt += 1
      problems[probname]["query"] = "".join(ls)

  # Dump the obfuscated one
  print(f"Total {obfus_cnt} problems among {len(problems)} successfully obfuscated")
  with open(sys.argv[3], "w") as wf:
    json.dump(problems, wf, indent=2)
