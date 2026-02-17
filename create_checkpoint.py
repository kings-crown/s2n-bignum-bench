import argparse
import hashlib
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fcntl
import termios
from concurrent.futures import ThreadPoolExecutor, as_completed

SETUP_PREFIX_VERSION = 2


# (***************************************************************************)
# (*                      Small string / path helpers                        *)
# (***************************************************************************)

def ocaml_string(value: str) -> str:
  return value.replace("\\", "\\\\").replace('"', '\\"')


# (***************************************************************************)
# (*                     Problem and template helpers                        *)
# (***************************************************************************)

def collect_query_files(target: Path) -> List[Path]:
  if target.is_dir():
    return sorted(target.rglob("query.txt"))
  if target.is_file():
    return [target]
  return []


def load_problems_index(path: Path) -> Dict[str, dict]:
  if not path.exists():
    return {}
  try:
    with path.open("r", encoding="utf-8") as f:
      data = json.load(f)
    return data if isinstance(data, dict) else {}
  except (OSError, json.JSONDecodeError) as exc:
    print(f"Warning: failed to load problems.json from {path}: {exc}")
    return {}


def template_info_for_problem(problem_id: str, problems_index: Dict[str, dict],
                              repo_root: Path) -> Optional[Tuple[Path, int]]:
  data = problems_index.get(problem_id)
  if not isinstance(data, dict):
    return None
  locations = data.get("inlined_locations")
  if not locations:
    return None
  template_rel, line = locations[0]
  try:
    line_num = int(line)
  except (TypeError, ValueError):
    return None
  template_path = (repo_root / template_rel).resolve()
  if not template_path.exists():
    return None
  return template_path, line_num


# (***************************************************************************)
# (*                         Setup file generation                           *)
# (***************************************************************************)

def ensure_setup_prefix(template_path: Path, line: int, output_path: Path) -> None:
  output_path.parent.mkdir(parents=True, exist_ok=True)
  lines = template_path.read_text(encoding="utf-8").splitlines()
  line = max(0, min(line, len(lines)))
  content = "\n".join(lines[:line])
  if content and not content.endswith("\n"):
    content += "\n"
  preamble = '#load "unix.cma";;\n\n'
  if not content.lstrip().startswith('#load "unix.cma";;'):
    content = preamble + content
  output_path.write_text(content, encoding="utf-8")


def ensure_setup_file(source_path: Path, output_path: Path) -> None:
  output_path.parent.mkdir(parents=True, exist_ok=True)
  content = source_path.read_text(encoding="utf-8")
  preamble = '#load "unix.cma";;\n\n'
  if not content.lstrip().startswith('#load "unix.cma";;'):
    content = preamble + content
  output_path.write_text(content, encoding="utf-8")


# (***************************************************************************)
# (*                        Checkpoint path helpers                          *)
# (***************************************************************************)

def checkpoint_dir_for(script_path: Path) -> Path:
  return Path(str(script_path) + ".ckpt")


def checkpoint_ready(script_path: Path) -> bool:
  return script_path.exists() and checkpoint_dir_for(script_path).is_dir()


def checkpoint_key(value: str) -> str:
  versioned = f"v{SETUP_PREFIX_VERSION}:{value}"
  return hashlib.sha256(versioned.encode("utf-8")).hexdigest()[:12]


def clean_stale_checkpoint_pairs(checkpoint_root: Path) -> Tuple[int, int]:
  """Remove orphaned checkpoint dirs/scripts so future builds do not abort early."""
  removed_dirs = 0
  removed_scripts = 0

  for ckpt_dir in checkpoint_root.glob("ckpt-*.ckpt"):
    script = ckpt_dir.with_suffix("")
    if script.exists():
      continue
    try:
      shutil.rmtree(ckpt_dir)
      removed_dirs += 1
    except Exception:
      continue

  for script in checkpoint_root.glob("ckpt-*"):
    if script.suffix:  # skip *.ckpt matches covered above
      continue
    ckpt_dir = script.with_suffix(".ckpt")
    if ckpt_dir.exists():
      continue
    try:
      script.unlink()
      removed_scripts += 1
    except Exception:
      continue

  return removed_dirs, removed_scripts


def ensure_problem_marker(problem_id: str, marker_path: Path) -> None:
  marker_path.parent.mkdir(parents=True, exist_ok=True)
  problem_literal = ocaml_string(problem_id)
  content = (
      f'let the_problem_name = "{problem_literal}";;\n'
      'let () = Printf.printf "[checkpoint] problem=%s\\n%!" the_problem_name;;\n'
  )
  marker_path.write_text(content, encoding="utf-8")


def build_checkpoint_snippet(setup_path: Path, repo_root: Path,
                             problem_marker_path: Optional[Path] = None) -> str:
  s2n_root = repo_root / "s2n-bignum"
  s2n_root_str = ocaml_string(str(s2n_root))
  setup_str = ocaml_string(str(setup_path))
  snippet = (
      f'Sys.chdir "{s2n_root_str}"; '
      f'load_path := ("{s2n_root_str}") :: !load_path; '
      f'loadt "{setup_str}"'
  )
  if problem_marker_path is not None:
    marker_str = ocaml_string(str(problem_marker_path))
    snippet += f'; loadt "{marker_str}"'
  return snippet


# (***************************************************************************)
# (*                      Checkpoint creation helpers                        *)
# (***************************************************************************)

def run_command_with_pty(cmd: List[str], cwd: Optional[str], env: dict,
                         max_output_bytes: int) -> Tuple[int, str]:
  master_fd, slave_fd = pty.openpty()
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

  output = bytearray()
  while True:
    rlist, _, _ = select.select([master_fd], [], [], 0.1)
    if master_fd in rlist:
      try:
        chunk = os.read(master_fd, 8192)
      except OSError:
        chunk = b""
      if chunk:
        output.extend(chunk)
        if len(output) > max_output_bytes:
          output = output[-max_output_bytes:]
      elif proc.poll() is not None:
        break
    elif proc.poll() is not None:
      break

  os.close(master_fd)
  proc.wait()
  return proc.returncode, output.decode("utf-8", errors="replace")


def ensure_checkpoint(script_path: Path, setup_path: Path, repo_root: Path,
                      hol_light_dir: Path,
                      problem_marker_path: Optional[Path] = None) -> None:
  if checkpoint_ready(script_path):
    return
  ckpt_dir = checkpoint_dir_for(script_path)
  if script_path.exists() or ckpt_dir.exists():
    raise RuntimeError(
        f"Stale checkpoint path exists: {script_path} or {ckpt_dir}. "
        "Remove it before recreating."
    )
  snippet = build_checkpoint_snippet(setup_path, repo_root, problem_marker_path)
  cmd = [
      str(hol_light_dir / "make-checkpoint.sh"),
      str(script_path),
      snippet,
  ]
  print(f"[checkpoint] creating {script_path}")
  env = os.environ.copy()
  env["LINE_EDITOR"] = "env"
  env["DMTCP_COORD_HOST"] = "127.0.0.1"
  env.pop("DMTCP_COORD_PORT", None)
  returncode, output_tail = run_command_with_pty(
      cmd=cmd,
      cwd=str(hol_light_dir),
      env=env,
      max_output_bytes=20000,
  )
  if returncode != 0:
    raise RuntimeError(
        f"make-checkpoint.sh failed for {script_path} (rc={returncode}).\n"
        f"{output_tail}"
    )
  deadline = time.time() + 60
  while time.time() < deadline:
    if script_path.exists() and ckpt_dir.is_dir():
      break
    time.sleep(0.2)
  if not script_path.exists():
    raise RuntimeError(
        f"Checkpoint script was not created: {script_path}. "
        "make-checkpoint.sh returned but the wrapper script is missing.\n"
        f"{output_tail}"
    )


def clean_stale_checkpoint(script_path: Path) -> bool:
  ckpt_dir = checkpoint_dir_for(script_path)
  removed = False
  if script_path.exists() and not ckpt_dir.is_dir():
    script_path.unlink()
    removed = True
  if ckpt_dir.exists() and not script_path.exists():
    shutil.rmtree(ckpt_dir)
    removed = True
  return removed


# (***************************************************************************)
# (*                  Parallel checkpoint build orchestration                *)
# (***************************************************************************)

def kill_stale_make_checkpoint_procs(repo_root: Path,
                                     max_age_secs: int) -> List[int]:
  """Terminate lingering hol-light/make-checkpoint.sh processes older than N seconds."""
  target = str((repo_root / "hol-light" / "make-checkpoint.sh").resolve())
  killed: List[int] = []
  try:
    ps = subprocess.run(
        ["ps", "-eo", "pid,etimes,cmd"],
        text=True,
        capture_output=True,
        check=True,
    )
  except Exception as exc:
    print(f"Warning: failed to list processes for cleanup: {exc}")
    return killed

  for line in ps.stdout.splitlines()[1:]:
    parts = line.strip().split(None, 2)
    if len(parts) < 3:
      continue
    pid_txt, age_txt, cmd = parts
    if "make-checkpoint.sh" not in cmd:
      continue
    if target not in cmd:
      continue
    try:
      age = int(age_txt)
      pid = int(pid_txt)
    except ValueError:
      continue
    if age < max_age_secs or pid == os.getpid():
      continue
    try:
      os.kill(pid, signal.SIGTERM)
      killed.append(pid)
    except ProcessLookupError:
      continue
    except Exception:
      continue

  if killed:
    time.sleep(0.5)
    for pid in list(killed):
      try:
        os.kill(pid, 0)
      except ProcessLookupError:
        continue
      except Exception:
        continue
      try:
        os.kill(pid, signal.SIGKILL)
      except Exception:
        pass
  return killed


def run_checkpoint_tasks(tasks: List[Tuple[Path, Path, Path]], repo_root: Path,
                         hol_light_dir: Path, workers: int) -> None:
  if not tasks:
    return
  if workers <= 1:
    for script_path, setup_path, marker_path in tasks:
      ensure_checkpoint(
          script_path,
          setup_path,
          repo_root,
          hol_light_dir,
          marker_path,
      )
    return
  with ThreadPoolExecutor(max_workers=workers) as executor:
    future_to_script = {
        executor.submit(
            ensure_checkpoint,
            script_path,
            setup_path,
            repo_root,
            hol_light_dir,
            marker_path,
        ): script_path
        for script_path, setup_path, marker_path in tasks
    }
    for future in as_completed(future_to_script):
      script_path = future_to_script[future]
      future.result()


def write_checkpoint_manifest(problems: List[dict], manifest_path: Path) -> None:
  manifest = {}
  if manifest_path.exists():
    try:
      manifest = json.load(manifest_path.open("r", encoding="utf-8"))
      if not isinstance(manifest, dict):
        manifest = {}
    except Exception:
      manifest = {}
  for info in problems:
    ckpt = info.get("checkpoint_path")
    pid = info.get("problem_id")
    if ckpt and pid:
      manifest[pid] = str(Path(ckpt).resolve())
  manifest_path.parent.mkdir(parents=True, exist_ok=True)
  manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


# (***************************************************************************)
# (*                                CLI                                      *)
# (***************************************************************************)

def build_tasks_per_problem(problems: List[dict], checkpoint_root: Path) -> Tuple[List[Tuple[Path, Path, Path]], List[dict]]:
  setup_inputs: Dict[Path, Tuple[Path, int]] = {}
  setup_file_inputs: Dict[Path, Path] = {}
  for info in problems:
    source_setup = info["query_path"].parent / "setup.ml"
    if source_setup.exists():
      stat = source_setup.stat()
      setup_key = f"{source_setup}:{stat.st_mtime_ns}:{stat.st_size}"
      setup_slug = checkpoint_key(setup_key)
      setup_path = checkpoint_root / f"setup-{setup_slug}.ml"
      setup_file_inputs.setdefault(setup_path, source_setup)
    else:
      template_info = info["template_info"]
      if template_info is None:
        raise RuntimeError(f"Missing setup.ml and template info for {info['problem_id']}")
      template_path, line = template_info
      setup_key = f"{template_path}:{line}"
      setup_slug = checkpoint_key(setup_key)
      setup_path = checkpoint_root / f"setup-{setup_slug}.ml"
      setup_inputs.setdefault(setup_path, (template_path, line))
    info["setup_path"] = setup_path

  for setup_path, source_path in setup_file_inputs.items():
    ensure_setup_file(source_path, setup_path)
  for setup_path, (template_path, line) in setup_inputs.items():
    ensure_setup_prefix(template_path, line, setup_path)

  tasks: List[Tuple[Path, Path, Path]] = []
  seen_scripts = set()
  for info in problems:
    setup_path = info["setup_path"]
    ckpt_key = f"{info['problem_id']}:{setup_path}"
    ckpt_slug = checkpoint_key(ckpt_key)
    script_path = checkpoint_root / f"ckpt-{ckpt_slug}"
    marker_path = checkpoint_root / f"problem-{ckpt_slug}.ml"
    info["checkpoint_path"] = script_path
    info["checkpoint_managed"] = True
    info["problem_marker_path"] = marker_path
    ensure_problem_marker(info["problem_id"], marker_path)
    if script_path not in seen_scripts:
      tasks.append((script_path, setup_path, marker_path))
      seen_scripts.add(script_path)
  return tasks, problems


def main() -> int:
  parser = argparse.ArgumentParser(
      description="Build HOL Light checkpoints without running the LLM solver."
  )
  parser.add_argument(
      "path",
      help=("Path to a query.txt file or a directory containing per-problem "
            "subdirectories with query.txt files."),
  )
  parser.add_argument(
      "--problems-json",
      default="problems.json",
      help="Path to problems.json used to map problems to templates.",
  )
  parser.add_argument(
      "--checkpoint-root",
      default="checkpoint_cache",
      help="Directory under the repo root where cached checkpoints and setups live.",
  )
  parser.add_argument(
      "--checkpoint-workers",
      type=int,
      default=1,
      help="Number of parallel workers when building checkpoints.",
  )
  parser.add_argument(
      "--kill-stale-checkpoint-procs",
      action="store_true",
      help="Terminate lingering make-checkpoint.sh processes older than --stale-proc-age seconds.",
  )
  parser.add_argument(
      "--stale-proc-age",
      type=int,
      default=300,
      help="Age threshold in seconds for make-checkpoint.sh to be considered stale.",
  )
  parser.add_argument(
      "--single-problem",
      default=None,
      help="Restrict checkpointing to one problem id (directory name under problems/).",
  )
  parser.add_argument(
      "--force-rebuild",
      action="store_true",
      help="Delete any existing checkpoint script/dir before rebuilding it.",
  )

  args = parser.parse_args()
  if args.checkpoint_workers < 1:
    print("--checkpoint-workers must be >= 1")
    return 1

  repo_root = Path(__file__).resolve().parent
  hol_light_dir = repo_root / "hol-light"

  if args.kill_stale_checkpoint_procs:
    killed = kill_stale_make_checkpoint_procs(
        repo_root=repo_root,
        max_age_secs=args.stale_proc_age,
    )
    if killed:
      print(f"[checkpoint] killed {len(killed)} stale make-checkpoint.sh processes "
            f"older than {args.stale_proc_age}s")

  target = Path(args.path)
  query_files = collect_query_files(target)
  if not query_files:
    print(f"No query.txt files found under {target}")
    return 1

  problems_index = load_problems_index((repo_root / args.problems_json).resolve())

  problems: List[dict] = []
  for query_path in query_files:
    problem_id = query_path.parent.name
    template_info = template_info_for_problem(problem_id, problems_index, repo_root)
    problems.append({
        "problem_id": problem_id,
        "query_path": query_path,
        "template_info": template_info,
    })

  if args.single_problem:
    problems = [p for p in problems if p["problem_id"] == args.single_problem]
    if not problems:
      print(f"Problem not found: {args.single_problem}")
      return 1

  checkpoint_root = (repo_root / args.checkpoint_root).resolve()
  checkpoint_root.mkdir(parents=True, exist_ok=True)

  cleaned_dirs, cleaned_scripts = clean_stale_checkpoint_pairs(checkpoint_root)
  if cleaned_dirs or cleaned_scripts:
    print(f"[checkpoint] cleaned {cleaned_dirs} stale checkpoint dirs and "
          f"{cleaned_scripts} stale scripts under {checkpoint_root}")

  if not hol_light_dir.exists():
    print(f"hol-light directory not found: {hol_light_dir}")
    return 1

  tasks, problems = build_tasks_per_problem(problems, checkpoint_root)

  if args.force_rebuild:
    for script_path, _, _ in tasks:
      ckpt_dir = checkpoint_dir_for(script_path)
      if script_path.exists():
        script_path.unlink()
      if ckpt_dir.exists():
        shutil.rmtree(ckpt_dir)

  print(f"[checkpoint] building {len(tasks)} checkpoint(s) with "
        f"{args.checkpoint_workers} worker(s)")
  run_checkpoint_tasks(
      tasks=tasks,
      repo_root=repo_root,
      hol_light_dir=hol_light_dir,
      workers=args.checkpoint_workers,
  )

  manifest_path = checkpoint_root / "checkpoint_manifest.json"
  write_checkpoint_manifest(problems, manifest_path)

  ready = sum(1 for script_path, _, _ in tasks if checkpoint_ready(script_path))
  print(f"[checkpoint] completed: {len(tasks)} requested, {ready} ready.")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
