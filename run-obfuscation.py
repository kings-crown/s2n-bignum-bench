import argparse
import hashlib
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

let s2n_bignum_bench_rename_const_collisions: term -> term =
  fun t ->
    (* A bound-variable name that ALSO parses as a constant (e.g. the ARM
       register Q1 = QREG 1) is reread as that constant on parse, breaking the
       round-trip with a typechecking error. Names ending in underscore also
       need renaming: the typed printer emits (_:ty), but HOL lexes _: as one
       identifier. This includes wildcard binders in match expressions.
       Avoid all existing variable names, including unchanged inner binders,
       so fresh names cannot capture or be captured. *)
    let avoid = map (fun v -> fst (dest_var v)) (variables t) in
    let taken nm =
      mem nm avoid || can get_const_type nm ||
      List.exists (fun (alias,_) -> alias = nm) !the_interface in
    let counter = ref 0 in
    let rec fresh ty =
      let nm = "v" ^ string_of_int !counter in
      let _ = counter := !counter + 1 in
      if taken nm then fresh ty else mk_var (nm, ty) in
    let rec rn env t =
      match t with
      | Var (_, _)   -> (try assoc t env with Failure _ -> t)
      | Const (_, _) -> t
      | Comb (f, x)  -> mk_comb (rn env f, rn env x)
      | Abs (bv, body) ->
          let name = fst (dest_var bv) in
          if String.ends_with ~suffix:"_" name || can get_const_type name
          then let nv = fresh (type_of bv) in mk_abs (nv, rn ((bv,nv)::env) body)
          else mk_abs (bv, rn env body) in
    rn [] t;;

let s2n_bignum_bench_alpha_rename (seed:int): term -> term =
  fun t ->
    (* names already free in t -- never reuse one as a fresh bound name (capture guard) *)
    let avoid = setify (map (fun v -> fst (dest_var v)) (frees t)) in
    let counter = ref seed in
    let rec fresh ty =
      let nm = "v" ^ string_of_int !counter in
      counter := !counter + 1;
      if mem nm avoid then fresh ty else mk_var (nm, ty) in
    let rec rn env t =
      match t with
      | Var (_, _)   -> (try assoc t env with Failure _ -> t)
      | Const (_, _) -> t
      | Comb (f, x)  -> mk_comb (rn env f, rn env x)
      | Abs (bv, body) ->
          let nv = fresh (type_of bv) in
          mk_abs (nv, rn ((bv,nv)::env) body) in
    rn [] t;;

let s2n_bignum_bench_print_obfuscated_query (tier:int) (seed:int) (t:term) (output_txt_path:string) =
  let t1 = s2n_bignum_bench_rename_const_collisions
             (s2n_bignum_bench_remove_invented_types t) in
  let t' = if tier >= 2 then s2n_bignum_bench_alpha_rename seed t1 else t1 in
  let orgval = !print_types_of_subterms in
  let _ = print_types_of_subterms := 2 in
  let str_t' = string_of_term t' in
  (try
    let oc = open_out output_txt_path in
    (try
      let t'' = parse_term str_t' in
      (* Accept alpha-equivalence, not syntactic identity: GSPEC comprehensions
         and _MATCH desugar through generated bound variables (GEN%PVAR%NNNN),
         which the parser regenerates with fresh gensym names on re-read. *)
      if not (aconv t'' t') then
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

def write_query(f, problem_name, output_path, tier):
  query = problems[problem_name]["query"]

  # Wrap query in parentheses so function-application queries remain a single argument.
  # Some theorems don't necessarily start with the backtick-delimited, helps resove parsing issues.
  seed = int(hashlib.sha1(problem_name.encode()).hexdigest(), 16) % 100000
  # A new structure binding also separates us from preceding declarations
  # that legitimately omit ;;. A bare call can become their next argument.
  f.write(f'let () = s2n_bignum_bench_print_obfuscated_query {tier} {seed} ({query}) "{output_path}";;\n\n')

def valid_obfuscated_query(path):
  try:
    text = Path(path).read_text().strip()
  except FileNotFoundError:
    return False
  return bool(text) and text.startswith("`") and text.endswith("`")


def obfuscate(num_cores, tier, resume_dir=None):
  rundir = resume_dir
  if rundir is not None:
    if not Path(rundir).is_dir():
      raise ValueError(f"Resume directory does not exist: {rundir}")
    _, separator, saved_tier = Path(rundir).name.rpartition("-t")
    if separator and saved_tier.isdigit() and int(saved_tier) != tier:
      raise ValueError(f"Resume directory is tier {saved_tier}, but --tier is {tier}")

  templates = dict()

  for prob_id in problems:
    if rundir is not None and valid_obfuscated_query(
        Path(rundir) / (prob_id + ".obfus.txt")):
      continue
    file_path, linenum = problems[prob_id]["inlined_locations"][0]
    if file_path not in templates:
      templates[file_path] = [(prob_id, linenum)]
    else:
      templates[file_path].append((prob_id, linenum))

  if rundir is not None and not templates:
    print(f"All queries already stored at {rundir}; skipping compilation")
    return rundir

  timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
  if rundir is None:
    rundir = f"obfus-run-{timestamp}-t{tier}"
    os.makedirs(rundir)
    compiledir = f"obfus-compile-{timestamp}"
  else:
    # Keep completed sources/logs untouched and give the runner only retries.
    retry_stamp = datetime.now().strftime('%f')
    compiledir = f"obfus-compile-{timestamp}-resume-{retry_stamp}"
  os.makedirs(compiledir)

  print(f"Writing obfuscated query printer to {compiledir}")

  for t in templates:
    templates[t].sort(key=lambda x:x[1])

    with open(t, "r") as f:
      lines = list(f.readlines())

    with open(os.path.join(compiledir, t.replace("/", "_")), "w") as wf:
      # These printers run for their initialization effects and export nothing.
      # A truncated template can leave weak OCaml types that later uses in the
      # full source would specialize. Keep those bindings private so they do
      # not escape through the compiled module's interface.
      wf.write("module Obfuscated_queries : sig end = struct\n")
      prevline = 0
      for i in range(0, len(templates[t])):
        prob_id, linenum = templates[t][i]
        for j in range(prevline, linenum):
          wf.write(lines[j])

        if i == 0:
          wf.write(obfuscator_code)
        write_query(wf, prob_id, os.path.join("..", os.path.join(rundir, prob_id + ".obfus.txt")), tier)
        prevline = linenum
      wf.write("\nend;;\n")

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
  parser = argparse.ArgumentParser(description="Obfuscate benchmark queries")
  parser.add_argument('--tier', type=int, default=1)
  parser.add_argument('--resume', metavar='RUN_DIR',
                      help="Reuse completed queries; use the same input and tier after the original run finishes")
  parser.add_argument('input', help="Input problems.json")
  parser.add_argument('num_cores', type=int)
  parser.add_argument('output', help="Output problems.json")
  args = parser.parse_args()
  if args.num_cores < 1:
    parser.error("num_cores must be positive")

  with open(args.input, "r") as f:
    problems = json.load(f)

  try:
    obfus_run_dir = obfuscate(args.num_cores, args.tier, args.resume)
  except ValueError as error:
    parser.error(str(error))

  # check obfuscation results
  has_unobfus_res = False
  for p in problems:
    result_path = Path(obfus_run_dir) / (p + ".obfus.txt")
    if not result_path.is_file() or not result_path.read_text().strip():
      print(f"Error: Problem {p} not obfuscated!!")
      locations = problems[p].get("inlined_locations", [])
      fail_source = locations[0][0] if locations else "unknown template"
      print(f"There was a problem in building or running {fail_source}")
      has_unobfus_res = True

  if has_unobfus_res:
    exit(1)

  # Update problem
  obfus_cnt = 0
  for probname in problems:
    result_path = Path(obfus_run_dir) / (probname + ".obfus.txt")
    with open(result_path, "r") as f:
      ls = list(f.readlines())
      if not valid_obfuscated_query(result_path):
        print(f"Note: obfuscation of {probname} failed")
        continue

      obfus_cnt += 1
      problems[probname]["query"] = "".join(ls)

  # Dump the obfuscated one
  print(f"Total {obfus_cnt} problems among {len(problems)} successfully obfuscated")
  with open(args.output, "w") as wf:
    json.dump(problems, wf, indent=2)
