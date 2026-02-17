import argparse
import csv
import datetime
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple, List

import fcntl
import termios
import signal
import select

SETUP_PREFIX_VERSION = 2

def normalize_tactic(tactic: str) -> str:
  text = tactic.strip()
  if text.endswith(";;"):
    text = text[:-2].rstrip()
  return text

def ocaml_string(value: str) -> str:
  return value.replace("\\", "\\\\").replace('"', '\\"')

def ocaml_string_literal(value: str) -> str:
  return (
      value.replace("\\", "\\\\")
      .replace('"', '\\"')
      .replace("\n", "\\n")
      .replace("\r", "\\r")
      .replace("\t", "\\t")
  )

def strip_query_quotes(query_text: str) -> str:
  text = query_text.strip()
  if text.startswith("`") and text.endswith("`") and len(text) >= 2:
    return text[1:-1]
  return text

def build_attempt_ml(query_text: str, tactic: str, problem_id: str,
                     judge_path: Path, error_path: Path, timeout: int) -> str:
  query = strip_query_quotes(query_text)
  query_literal = ocaml_string_literal(query)
  tactic = normalize_tactic(tactic)
  problem_id_literal = ocaml_string(problem_id)
  judge = ocaml_string(str(judge_path))
  error = ocaml_string(str(error_path))
  return f"""#load "unix.cma";;

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
  | e -> ignore (Unix.alarm 0); raise e;;

let bench_run_with_error (problem_name:string) (query:term) (tac0:unit->tactic)
                         (output_txt_path:string) (error_txt_path:string) (timeout:int) =
  let oc = open_out output_txt_path in
  let ec = open_out error_txt_path in
  let axioms_before = axioms() in
  (try
     let tac:tactic = tac0 () in
     let _:thm = s2n_bignum_bench_timed_fun prove (query, tac) timeout in
     let axioms_after = axioms() in
     output_string oc (if axioms_before = axioms_after then "OK" else "CHEATING")
   with
     | Failure msg -> output_string oc "FAIL"; output_string ec msg
     | S2n_bignum_bench_timeout -> output_string oc "TIMEOUT"; output_string ec "TIMEOUT"
     | e -> output_string oc "ERROR"; output_string ec (Printexc.to_string e));
  close_out oc; close_out ec;;

let query = parse_term "{query_literal}";;

bench_run_with_error "{problem_id_literal}" query
  (fun () ->
     let expected_problem_name = "{problem_id_literal}" in
     let actual_problem_name = (try Some the_problem_name with _ -> None) in
     (match actual_problem_name with
      | Some name when name = expected_problem_name -> ()
      | Some name ->
          raise (Invalid_argument
            (Printf.sprintf "checkpoint-mismatch: expected %s got %s"
               expected_problem_name name))
      | None ->
          raise (Invalid_argument "checkpoint missing the_problem_name"));
     {tactic})
  "{judge}" "{error}" {timeout};;

#quit;;
"""

def checkpoint_key(value: str) -> str:
  versioned = f"v{SETUP_PREFIX_VERSION}:{value}"
  return hashlib.sha256(versioned.encode("utf-8")).hexdigest()[:12]


def load_problems_index(path: Path) -> Dict[str, dict]:
  if not path.exists():
    return {}
  try:
    with path.open("r", encoding="utf-8") as f:
      data = json.load(f)
    return data if isinstance(data, dict) else {}
  except (OSError, json.JSONDecodeError):
    return {}


def template_info_for_problem(problem_id: str, problems_index: Dict[str, dict],
                              repo_root: Path) -> Optional[Tuple[Path, int]]:
  data = problems_index.get(problem_id)
  if not isinstance(data, dict):
    return None
  locations = data.get("inlined_locations")
  if not locations:
    return None
  try:
    template_rel, line = locations[0]
  except Exception:
    return None
  try:
    line_num = int(line)
  except (TypeError, ValueError):
    return None
  template_path = (repo_root / template_rel).resolve()
  if not template_path.exists():
    return None
  return template_path, line_num

def load_timeouts(path: Path) -> Dict[str, int]:
  if not path.exists():
    return {}
  try:
    with path.open("r", encoding="utf-8") as f:
      data = json.load(f)
    if not isinstance(data, dict):
      return {}
    out: Dict[str, int] = {}
    for k, v in data.items():
      try:
        out[str(k)] = int(v)
      except (TypeError, ValueError):
        continue
    return out
  except (OSError, json.JSONDecodeError):
    return {}


def load_manifest(path: Path) -> Dict[str, str]:
  if not path.exists():
    return {}
  try:
    with path.open("r", encoding="utf-8") as f:
      data = json.load(f)
    return data if isinstance(data, dict) else {}
  except (OSError, json.JSONDecodeError):
    return {}


def syntax_check(problem_id: str, tactic_text: str, problems_index: Dict[str, dict],
                 repo_root: Path) -> bool:
  """Run synchk.sh on a minimal ML fragment to catch parse errors; True if OK."""
  info = problems_index.get(problem_id)
  if not info:
    return True  # no metadata; skip check
  locs = info.get("inlined_locations") or []
  if not locs:
    return True
  first = locs[0]
  if not isinstance(first, (list, tuple)) or len(first) < 2:
    return True
  template_rel, line = first[0], first[1]
  try:
    line_num = int(line)
  except Exception:
    return True
  template_path = (repo_root / template_rel).resolve()
  if not template_path.exists():
    return True

  synchk = repo_root / "synchk.sh"
  if not synchk.exists():
    return True

  with template_path.open("r", encoding="utf-8") as src, \
       tempfile.NamedTemporaryFile("w", suffix=".synchk.ml", delete=False, encoding="utf-8") as tmp:
    lines = src.readlines()
    tmp.writelines(lines[:line_num])
    # Mirror bench_run's expected (unit -> tactic) shape so ill-typed tactics fail here.
    tmp.write(f"let _ : unit -> tactic = (fun () -> {tactic_text});;\n")
    tmp_path = Path(tmp.name)

  try:
    result = subprocess.run(["bash", str(synchk), str(tmp_path)], capture_output=True)
    return result.returncode == 0
  finally:
    try:
      tmp_path.unlink()
    except Exception:
      pass


def timeout_for_category(category: str, timeouts: Dict[str, int],
                         default_timeout: int) -> int:
  if category in timeouts:
    return timeouts[category]
  if "generic" in timeouts:
    return timeouts["generic"]
  return default_timeout


def checkpoint_dir_for(script_path: Path) -> Path:
  return Path(str(script_path) + ".ckpt")


def checkpoint_ready(script_path: Path) -> bool:
  return script_path.exists() and checkpoint_dir_for(script_path).is_dir()


def resolve_restart_script(checkpoint_path: Path) -> Optional[Path]:
  restart_script = checkpoint_dir_for(checkpoint_path) / "dmtcp_restart_script.sh"
  if restart_script.exists():
    return restart_script
  return None


def pick_free_port() -> int:
  sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  sock.bind(("127.0.0.1", 0))
  port = sock.getsockname()[1]
  sock.close()
  return port


def run_checkpoint_attempt(checkpoint_path: Path, ml_snippet: str,
                           stdout_path: Path, stderr_path: Path,
                           timeout: int, use_pty: bool):
  """Execute the checkpoint script (or its dmtcp restart wrapper) with the ML snippet using a PTY."""
  env = os.environ.copy()
  env["LINE_EDITOR"] = "env"
  cmd = [str(checkpoint_path)]
  cwd = None
  restart_script = resolve_restart_script(checkpoint_path)
  debug_info = {
      "cmd": cmd,
      "cwd": cwd,
      "use_pty": True,
      "restart_script": str(restart_script) if restart_script else None,
      "coord_host": None,
      "coord_port": None,
      "timed_out": False,
      "returncode": None,
      "duration_secs": None,
  }
  if restart_script is not None:
    cmd = [str(restart_script)]
    cwd = str(restart_script.parent)
    env["DMTCP_COORD_HOST"] = "127.0.0.1"
    env["DMTCP_COORD_PORT"] = str(pick_free_port())
    debug_info["cmd"] = cmd
    debug_info["cwd"] = cwd
    debug_info["coord_host"] = env["DMTCP_COORD_HOST"]
    debug_info["coord_port"] = env["DMTCP_COORD_PORT"]

  master_fd, slave_fd = os.openpty()
  def _preexec() -> None:
    os.setsid()
    try:
      fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
    except Exception:
      pass
  proc = subprocess.Popen(
      cmd,
      stdin=slave_fd,
      stdout=slave_fd,
      stderr=slave_fd,
      env=env,
      cwd=cwd,
      preexec_fn=_preexec,
  )
  os.close(slave_fd)

  # Stream write of ML snippet
  data = ml_snippet.encode("utf-8")
  view = memoryview(data)
  total = 0
  while total < len(data):
    written = os.write(master_fd, view[total:])
    if written <= 0:
      break
    total += written

  stdout_file = stdout_path.open("w", encoding="utf-8", errors="replace")
  start = time.time()
  deadline = start + timeout if timeout and timeout > 0 else None
  timed_out = False
  while True:
    if deadline is not None and time.time() >= deadline:
      timed_out = True
      break
    rlist, _, _ = select.select([master_fd], [], [], 0.1)
    if master_fd in rlist:
      try:
        chunk = os.read(master_fd, 8192)
      except OSError:
        break
      if chunk:
        stdout_file.write(chunk.decode("utf-8", errors="replace"))
      elif proc.poll() is not None:
        break
    elif proc.poll() is not None:
      break

  stdout_file.close()
  os.close(master_fd)
  if timed_out and proc.poll() is None:
    try:
      os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
      pass
    try:
      proc.wait(timeout=5)
    except Exception:
      try:
        os.killpg(proc.pid, signal.SIGKILL)
      except Exception:
        pass
      try:
        proc.wait(timeout=2)
      except Exception:
        pass
  else:
    proc.wait()
  text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
  stderr_path.write_text("", encoding="utf-8")
  debug_info["timed_out"] = timed_out
  debug_info["duration_secs"] = round(time.time() - start, 3)
  debug_info["returncode"] = proc.returncode

  if restart_script is not None:
    try:
      subprocess.run(
          ["dmtcp_command", "-q"],
          env=env,
          cwd=cwd,
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
          timeout=5,
      )
    except Exception:
      pass
  return subprocess.CompletedProcess(cmd, proc.returncode, stdout=text, stderr=""), debug_info


def run_single(problem_id: str, answer_path: Path, checkpoint_path: Optional[Path],
               problem_timeout: int, args, repo_root: Path, run_dir: Path) -> Tuple[str, str, int, str]:
  """Run the tactic through the checkpoint; returns (problem_id, status, exit_code, log)."""
  problem_dir = repo_root / "problems" / problem_id
  query_path = problem_dir / "query.txt"
  if not query_path.exists():
    msg = f"query.txt not found for problem {problem_id} at {query_path}"
    return problem_id, "missing_query", 1, msg
  if not answer_path.exists():
    msg = f"answer file not found: {answer_path}"
    return problem_id, "missing_answer", 1, msg

  if checkpoint_path is None or not checkpoint_ready(checkpoint_path):
    msg = f"checkpoint missing or incomplete: {checkpoint_path}"
    print(f"[assess] {problem_id}: {msg}", flush=True)
    return problem_id, "missing_checkpoint", 1, msg

  attempts_root = run_dir / "attempts" / problem_id
  attempts_root.mkdir(parents=True, exist_ok=True)
  per_problem_dir = run_dir / "answers" / problem_id
  per_problem_dir.mkdir(parents=True, exist_ok=True)
  attempt_dir = attempts_root / "attempt-01"
  attempt_dir.mkdir(parents=True, exist_ok=True)

  tactic = answer_path.read_text(encoding="utf-8").strip()
  (attempt_dir / "answer.txt").write_text(tactic, encoding="utf-8")

  judge_path = attempt_dir / "judge.txt"
  error_path = attempt_dir / "hol_error.txt"
  stdout_path = attempt_dir / "hol_stdout.txt"
  stderr_path = attempt_dir / "hol_stderr.txt"

  attempt_ml = build_attempt_ml(
      query_text=query_path.read_text(encoding="utf-8"),
      tactic=tactic,
      problem_id=problem_id,
      judge_path=judge_path.resolve(),
      error_path=error_path.resolve(),
      timeout=problem_timeout,
  )

  proc, debug_info = run_checkpoint_attempt(
      checkpoint_path=checkpoint_path,
      ml_snippet=attempt_ml,
      stdout_path=stdout_path,
      stderr_path=stderr_path,
      timeout=problem_timeout + 30,
      use_pty=True,
  )

  if debug_info.get("timed_out") and not stderr_path.read_text(encoding="utf-8", errors="ignore").strip():
    stderr_path.write_text("Timeout while running checkpoint.", encoding="utf-8")

  status = "ERROR"
  if judge_path.exists():
    status = judge_path.read_text(encoding="utf-8", errors="ignore").strip()

  log = ""
  if stdout_path.exists():
    log += stdout_path.read_text(encoding="utf-8", errors="ignore")
  if stderr_path.exists():
    log += stderr_path.read_text(encoding="utf-8", errors="ignore")

  exit_code = 0 if status == "OK" else 1
  return problem_id, status.lower(), exit_code, log


def run_batch(args, repo_root: Path) -> int:
  base_dir = (repo_root / args.batch_run_dir).resolve()
  if not base_dir.is_dir():
    print(f"batch run dir not found: {base_dir}")
    return 1

  manifest_path = (repo_root / args.manifest).resolve()
  manifest = load_manifest(manifest_path)
  problems_index = load_problems_index((repo_root / "problems.json").resolve())
  timeouts = load_timeouts((repo_root / "timeouts.json").resolve())
  explicit_checkpoint = None
  if args.checkpoint:
    candidate = Path(args.checkpoint).expanduser().resolve()
    candidate_dir = Path(str(candidate) + ".ckpt")
    if candidate.exists() and candidate_dir.is_dir():
      explicit_checkpoint = candidate
      print(f"[batch] using explicit checkpoint {candidate}")
    else:
      print(f"[batch] provided checkpoint missing/incomplete, ignoring: {candidate}")

  problems = []
  syntax_pass = []
  syntax_fail = []
  problems_index = load_problems_index((repo_root / "problems.json").resolve())
  for problem_dir in sorted(base_dir.iterdir()):
    answer_path = problem_dir / "answer.txt"
    if not answer_path.exists():
      continue
    pid = problem_dir.name
    ckpt = str(explicit_checkpoint) if explicit_checkpoint else manifest.get(pid)
    ckpt_path = Path(ckpt) if ckpt else None
    if ckpt_path is None:
      print(f"[assess] {pid}: no checkpoint found in manifest/override", flush=True)
    elif not checkpoint_ready(ckpt_path):
      print(f"[assess] {pid}: checkpoint missing or incomplete: {ckpt_path}", flush=True)
    meta = problems_index.get(pid, {}) if isinstance(problems_index, dict) else {}
    category = meta.get("category", "generic")
    problem_timeout = timeout_for_category(category, timeouts, args.timeout)
    problems.append((pid, answer_path, ckpt_path, problem_timeout))

  if not problems:
    print("No answer.txt files found for batch.")
    return 1

  ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
  summary_path = Path(args.summary_csv) if args.summary_csv else (repo_root / "runs_assess" / f"assess-summary-{ts}.csv")
  summary_path.parent.mkdir(parents=True, exist_ok=True)

  run_tag = args.run_tag if args.run_tag else "assess"
  run_root = repo_root / args.run_root
  run_dir = run_root / f"run-{ts}-{run_tag}"
  run_dir.mkdir(parents=True, exist_ok=True)

  rows = []
  total = len(problems)
  passed_ids: List[str] = []
  failed_ids: List[str] = []
  for idx, (pid, answer_path, ckpt, problem_timeout) in enumerate(problems, start=1):
    print(f"[batch] assessing {idx}/{total} {pid}")
    tactic_text = answer_path.read_text(encoding="utf-8").strip()
    if not syntax_check(pid, tactic_text, problems_index, repo_root):
      rows.append([pid, "syntax_error", 1, "", ""])
      failed_ids.append(pid)
      syntax_fail.append(pid)
      continue
    syntax_pass.append(pid)
    _, status, exit_code, log = run_single(
        problem_id=pid,
        answer_path=answer_path,
        checkpoint_path=ckpt,
        problem_timeout=problem_timeout,
        args=args,
        repo_root=repo_root,
        run_dir=run_dir,
    )
    rows.append([pid, status, exit_code,
                 str((run_dir / "attempts" / pid).resolve()),
                 str((run_dir / "answers" / pid).resolve())])
    if status in ("ok", "pass"):
      passed_ids.append(pid)
    else:
      failed_ids.append(pid)

  with summary_path.open("w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["problem_id", "status", "exit_code", "attempts_path", "answers_path"])
    writer.writerows(rows)

  passes = sum(1 for row in rows if row[1] in ("ok", "pass"))
  fails = sum(1 for row in rows if row[1] not in ("ok", "pass"))
  print(f"[batch] summary written to {summary_path}")
  print(f"[batch] pass {passes}, fail {fails}")
  if passed_ids:
    print(f"[batch] passed ({len(passed_ids)}/{total}): " + ", ".join(passed_ids))
  print(f"[batch] syntax pass {len(syntax_pass)}, syntax fail {len(syntax_fail)}")
  if syntax_pass:
    print(f"[batch] syntax-passed: " + ", ".join(syntax_pass))
  if syntax_fail:
    print(f"[batch] syntax-failed: " + ", ".join(syntax_fail))
  return 0

def main() -> int:
  parser = argparse.ArgumentParser(description="Assess a tactic using existing checkpoint_retry flow.")
  parser.add_argument("problem_id", nargs="?", help="Problem id (directory name under problems/).")
  parser.add_argument("answer_path", nargs="?", help="Path to answer.txt to run.")
  parser.add_argument("--checkpoint",
                      help="Path to an existing checkpoint script to reuse; if missing, falls back to creating per-problem.")
  parser.add_argument("--batch-run-dir",
                      help="Directory containing per-problem subdirs with answer.txt (enables batch mode).")
  parser.add_argument("--summary-csv",
                      help="Optional summary CSV path for batch mode; defaults to runs_assess/assess-summary-<timestamp>.csv")
  parser.add_argument("--manifest",
                      default="checkpoint_cache/checkpoint_manifest.json",
                      help="Checkpoint manifest JSON mapping problem_id -> checkpoint path (used in batch mode).")
  parser.add_argument("--timeout", type=int, default=60,
                      help="HOL Light timeout seconds for the run.")
  parser.add_argument("--run-root", default="runs_assess",
                      help="Directory under repo root for run-* outputs.")
  parser.add_argument("--run-tag", default="assess",
                      help="Tag suffix for the run directory name.")
  args = parser.parse_args()

  repo_root = Path(__file__).resolve().parent

  if not args.batch_run_dir:
    print("Only batch mode is supported; please provide --batch-run-dir.")
    return 1

  return run_batch(args, repo_root)

if __name__ == "__main__":
  raise SystemExit(main())
