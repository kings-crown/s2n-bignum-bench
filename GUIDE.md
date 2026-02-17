# s2n-bignum-bench Comprehensive Guide

## 1. Project Overview
- **Goal**: benchmark tactic synthesis for AWS s2n-bignum proofs using HOL Light.
- **Inputs**: upstream HOL Light repo, s2n-bignum proofs, architecture-specific object files, and problem metadata.
- **Outputs**: generated problem corpus (`problems.json`), optional obfuscated variants,(After LLMs attempt to prove proofs) evaluator artefacts (`eval-*`), and proof verdicts.

## 2. Prerequisites
- Linux environment with `bash`, `git`, `make`, `gcc`, Python 3.8+.
- `opam` installed and on `PATH` (setup creates its own switch).
- System packages often required: `gmp`, `zip`, `rsync`, `pkg-config` (depends on distribution).
- Python packages: `requests`.
- Python: **3.9+** required (`collect-problems.py` uses `str.removesuffix`).
- GPU + CUDA stack if you intend to host a local LLM with `vllm serve` to attempt the problem set.
- Disk: ~20 GB free for cloned repos, build artefacts, and evaluation outputs.

## 3. Repository Layout
- `setup.sh` – clones/builds HOL Light and s2n-bignum, collects theorem dumps.
- `toplevel-thms/` – generated JSON/ML metadata grouped by architecture.
- `ml_files/` – inline `.ml` sources with proofs replaced by `CHEAT_TAC`.
- `objfiles/` – architecture-specific object files used when executing proofs.
- `collect-*.py`, `retrieve-problem.py`, `combine-answer-and-setup.py` – scripts composing the benchmark pipeline.
- `run-answers.sh`, `run-answers-bytecode.sh` – proof compilation/execution helpers (native first, bytecode-only fallback).
- `run-obfuscation.py`, `strip.sh`, `synchk.sh` – obfuscation, binary stripping, and syntax checking utilities.
- `template-cache.json` – auto-generated cache that preserves template-to-problem mappings between runs.
- `problems/` – generated per-problem template files (not checked in by default).
- `problems.json` – generated corpus metadata.
- `prompt-basic.txt` – baseline functional-correctness prompt text.
- `timeouts.json` – per-category timeout overrides used by runners.

## 4. Initial Setup (`setup.sh`)
1. From the repo root: `./setup.sh [--reuse] <arch> <num-cores>`.
2. `--reuse` keeps existing `hol-light/` and `s2n-bignum/` directories; omit for a clean rebuild.
3. Traces are optional. To populate `trace-logs/<arch>/`, run `make run_proofs -j<num-cores>` inside `s2n-bignum/<arch>` after setup finishes.
4. `<arch>` must be `arm` or `x86`; build products land under matching subdirectories.
5. `<num-cores>` controls parallelism for `make`. Aim for `RAM_in_GB / 6` to avoid swapping.
6. Outputs:
    - `hol-light/` pinned to commit `0a5e9…` with TacticTrace built.
    - `s2n-bignum/` pinned to `002fdb…` with proof artefacts.
    - `toplevel-thms/<arch>/` containing theorem dumps and inline `.ml` files.
   - `trace-logs/<arch>/` containing per-theorem trace dumps only if you run `make run_proofs -j<num-cores>` inside `s2n-bignum/<arch>`.
   - `objfiles/<arch>/` populated with object files copied from s2n-bignum builds.
7. Re-run with a different architecture to populate both `arm` and `x86`. Merge directories manually if you want a combined corpus.

## 5. Building the Problem Corpus
1. Generate tasks: `python3 collect-problems.py [--quiet] toplevel-thms/ problems.json ml_files`.
2. `--quiet` suppresses per-theorem logging; missing `.nolinenum.json` / `.ml` companions are skipped with warnings.
3. Script actions:
   - Reads theorem metadata, filters non-functional goals, categorises problems.
   - Writes inline `.ml` sources with `CHEAT_TAC` placeholders to `ml_files/<arch>/`.
   - Records goals, categories, and template locations in `problems.json`.
4. Inspect `problems.json` to verify counts per category.

## 6. Optional Query Obfuscation
1. `python3 run-obfuscation.py problems.json <num-cores> problems-obfuscated.json`.
2. Creates temporary compile/run directories under `obfus-*`, emits obfuscated queries.
3. Swap files if desired:
   ```bash
   mv problems.json problems-unobfuscated.json
   mv problems-obfuscated.json problems.json
   ```
4. On failure the script now reports the specific template path that failed compilation.

## 7. Manual Workflow for Solving Problems
1. **Discover problems**: `python3 retrieve-problem.py list [--category CATEGORY]`.
2. **Materialise problems**: `python3 retrieve-problem.py retrieve --outputdir workdir [--name PID | --category CATEGORY]`.
   - Produces `setup.ml` and `query.txt` under `workdir/<problem-id>/`.
3. ### **Author proofs**: Add/edit `answer.txt` alongside each setup/query pair under the appropriate problem folders. This is where LLMs will insert their answers for evaluation.
   - Expected layout for this pipeline: `workdir/<problem-id>/{setup.ml,query.txt,answer.txt}`.
   - The `<problem-id>` directory name must match the identifier from `problems.json`.
4. **Combine & validate**: `python3 combine-answer-and-setup.py workdir <num-cores>`.
   - Performs syntax checks (`synchk.sh`) and writes `eval-<timestamp>/`.
   - Uses `template-cache.json` to reuse portfolio layouts when possible.
5. **Execute tactics**:
   - Native first: `./run-answers.sh eval-<timestamp> <cores>`
   - Bytecode-only fallback (no native ocamlopt): `./run-answers-bytecode.sh eval-<timestamp> <cores>`
   - Per-problem compile/run logs are written next to each generated `.ml` inside `eval-<timestamp>/`, e.g. `eval-<timestamp>/<template>.run.outlog`.
6. **Collect verdicts**: `python3 collect-verdicts.py eval-<timestamp> results.csv`.
   - Judge files (`*.judge.txt`) record `OK`, `FAIL`, `CHEATING`, or `ERROR`.

## 8. Notes on Provided Assets
- `problems/` and `problems.json` are generated by `collect-problems.py` and may be absent until you run it.
- `prompt-basic.txt` is a starting prompt for functional-correctness tasks; adjust it if you tweak evaluation policies.
- `timeouts.json` lets you set per-category timeouts for `run-answers*.sh` without changing the scripts.
- `template-cache.json` is safe to delete when portfolio layouts become stale.
- `runs/` directories are created only when you execute workflows that emit answers/evaluations; they are not committed.

## 9. Maintenance Tips
- Remove `template-cache.json` if you change `problems.json` or want to rebuild template portfolios from scratch.
- The obfuscation step relies on the bytecode runner; ensure `ocamlc` is available within the Hol Light opam switch.
- Keep `hol-light/` and `s2n-bignum/` at the pinned commits unless you are prepared to refresh `s2n-bignum.patch` and re-verify scripts.

## 10. Debugging Checklist
1. **Setup failures**
   - Verify network access when cloning repositories.
   - Confirm opam initialization succeeded (`eval $(opam env --switch ...)`).
   - Inspect `hol-light/TacticTrace/build.log` or `s2n-bignum/<arch>/Makefile` output for compiler errors.
2. **`collect-problems.py` complaints**
   - Missing `.json` or `.ml` files indicate `setup.sh` didn’t finish; confirm `toplevel-thms/<arch>/` exists.
   - Run with `--quiet` off to see which theorem triggered an issue.
3. **Obfuscation errors**
   - Check the reported template path (new diagnostic). Re-run the failing `.ml` with `ocamlc` manually inside the Hol Light switch.
4. **Combine/evaluate issues**
   - Syntax errors: inspect `eval-*/log-*.txt` for the listing and the generated `.synchk.ml` file.
   - Runtime exceptions: open `objfiles/<arch>/<problem>.run.errlog`.
   - Timeouts: increase timeout in the .json file or narrow the proof search.
5. **Runner limits**
   - Adjust `timeouts.json` before invoking the runners.
   - Use `run-answers-bytecode.sh` if native ocamlopt fails on your platform.
6. **Caching anomalies**
   - Delete `template-cache.json` if templates no longer match the current `problems.json`.
   - Clean `runs/` directories if you want to restart from scratch.
7. **Permission issues**
   - Some workflows (e.g., `python3 -m compileall`) create `__pycache__`; ensure the repository directory is writable when running locally.

## 11. Checkpointed HOL Light Sessions
- Build checkpoints without running the solver: `python3 create_checkpoint.py <path-to-query.txt-or-dir> [--checkpoint-root checkpoint_cache] [--checkpoint-workers N] [--single-problem PID] [--force-rebuild]`. Outputs wrapper scripts `ckpt-<hash>` plus `checkpoint_manifest.json` under the chosen cache.
- Per-problem metadata: each checkpoint now records `the_problem_name` (printed on load). During assessment the runner checks this against the expected problem id and fails fast with `ERROR` if they do not match, preventing cross-problem reuse.
- Reuse in assessment: `assess_answer.py` batch mode consumes the manifest (`checkpoint_cache_per_problem/checkpoint_manifest.json` by default). It also accepts `--checkpoint` for an explicit path; the checkpoint must include the matching `the_problem_name`.
- When you use a non-default `--checkpoint-root` with `create_checkpoint.py`, pass the resulting manifest to `assess_answer.py` via `--manifest <checkpoint_root>/checkpoint_manifest.json` so it can locate the checkpoints.
- Troubleshooting: if you see “checkpoint-mismatch” or “checkpoint missing the_problem_name”, rebuild with `--force-rebuild` to refresh the saved OCaml image and marker file, then rerun.
- Expected layout for batch assessment: `--batch-run-dir <answers-dir>` where `<answers-dir>/<problem-id>/answer.txt` exists for each problem you want to assess. `assess_answer.py` reads `problems/` for `query.txt` and uses the manifest to map problem ids to checkpoints.
- DMTCP dependency: checkpoint creation and reuse requires DMTCP to be installed and on `PATH`. See project docs:
  - `https://github.com/dmtcp/dmtcp`

### Assessment modes (Pass@K vs Pass@1)
- **Checkpointed assessment (Pass@K / many retries)**: Use `create_checkpoint.py` + `assess_answer.py` to amortize HOL Light startup across many candidate answers. This is ideal when you generate multiple attempts per problem and want fast, repeated evaluation with the same loaded context.
- **Static combine-and-run (Pass@1)**: Use `combine-answer-and-setup.py` + `run-answers*.sh` when you have a single answer per problem and want a stable, one-shot benchmark run.
