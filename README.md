# s2n-bignum-bench

A practical benchmark for evaluating low-level code reasoning of LLMs through HOL Light tactic synthesis on cryptographic proofs from AWS [s2n-bignum](https://github.com/awslabs/s2n-bignum).

[Paper]() | [Leaderboard](https://kings-crown.github.io/s2n-bignum-leaderboard/) | [GUIDE.md](GUIDE.md)

## Quick Start for Challengers

Each problem gives you a HOL Light goal (a boolean term) and asks for a tactic proof. Here's an example:

**Goal:**
```
`x * (y + z) = x * z + x * y`
```

**Expected answer** — a HOL Light tactic chain:
```
REWRITE_TAC[LEFT_ADD_DISTRIB] THEN
GEN_REWRITE_TAC LAND_CONV [ADD_SYM] THEN
REFL_TAC
```

The full workflow:

```bash
# 1. Clone and set up (builds HOL Light + s2n-bignum, ~20 GB disk, ~1hr)
git clone https://github.com/kings-crown/s2n-bignum-bench
cd s2n-bignum-bench
./setup.sh arm 4        # then: ./setup.sh --reuse x86 4
python3 collect-problems.py toplevel-thms/ problems.json ml_files

# 2. Retrieve problems as a CSV for your LLM
python3 retrieve-problem.py retrieve --outputdir workdir --csv problems.csv --csv-only

# 3. Have your model generate answers (one per problem)
#    Output format: CSV with columns problem_id, category, query, answer

# 4. Evaluate answers
python3 retrieve-problem.py retrieve --outputdir workdir
#    Place each answer in workdir/<problem-id>/answer.txt
python3 combine-answer-and-setup.py workdir 8
./run-answers.sh eval-<timestamp> 8
python3 collect-verdicts.py eval-<timestamp> results.csv

# 5. Submit to the leaderboard
#    Zip your answers CSV and submit at:
#    https://github.com/kings-crown/s2n-bignum-leaderboard/issues/new?template=leaderboard-submission.yml
```

See [GUIDE.md](GUIDE.md) for detailed instructions, checkpointed assessment (Pass@K), obfuscation, and debugging.

---

## Preparation

### 1. Build HOL Light, s2n-bignum, and collect top-level theorems
```
./setup.sh [--reuse] <arch(arm|x86)> <cores>
```
- `--reuse` keeps existing `hol-light/` and `s2n-bignum/` if present.
- `<cores>`: each proof can use ~5 GB RAM; a safe rule is `RAM_GB / 6`.
- Outputs:
  - `hol-light/` with TacticTrace built.
  - `s2n-bignum/` with the chosen architecture’s artefacts.
  - `toplevel-thms/<arch>/` theorem metadata and inline `.ml`.
  - `trace-logs/<arch>/` (only if you later run `make run_proofs -j<cores>` inside `s2n-bignum/<arch>`).
  - `objfiles/<arch>/` object files copied from the build.
- Traces are optional; run `(cd s2n-bignum/<arch> && make run_proofs -j<cores>)` if you need `trace-logs/<arch>/`.
- Problems differ by architecture; merge `toplevel-thms/` and `objfiles/` if you want a combined corpus.

### 2. Generate the problem set
```
python3 collect-problems.py [--quiet] toplevel-thms/ problems.json ml_files
```
- Produces `problems.json` (corpus metadata) and `ml_files/<arch>/` with `CHEAT_TAC` placeholders.

### 3. (Optional) Obfuscate the problems -- Work in Progress
```
python3 run-obfuscation.py problems.json <cores> problems-obfuscated.json
mv problems.json problems-unobfuscated.json
mv problems-obfuscated.json problems.json
```
- On failure, the script reports the template path that failed to compile.

## Usage

### Assessment modes
There are two supported assessment workflows:

1. **Static combine-and-run (best for Pass@1 benchmarking)**  
   - Use `combine-answer-and-setup.py` to generate a single `eval-<timestamp>/` from one answer per problem, then run it with `run-answers*.sh`.  
   - This is the simplest, most stable path for one-shot (Pass@1) evaluations of a fixed benchmark submission.

2. **Checkpointed assessment (best for Pass@K or many retries) -- Work in Progress**  
   - Build checkpoints with `create_checkpoint.py`, then evaluate with `assess_answer.py --batch-run-dir ...` using the generated manifest.  
   - This amortizes HOL Light startup and loading costs across many candidate answers, which is especially effective when you are testing multiple attempts per problem (Pass@K-style workflows).

### Retrieve problems
```
python3 retrieve-problem.py list [--category CATEGORY]
python3 retrieve-problem.py retrieve --outputdir <dir> [--name PID | --category CATEGORY]
```
Generates `setup.ml` and `query.txt` under `<dir>/<problem-id>/`.

### Write your answer
Add `answer.txt` beside each `setup.ml` and `query.txt`.

### Combine and validate
```
python3 combine-answer-and-setup.py <dir> <num-cores>
```
Creates `eval-<timestamp>/`, runs syntax checks (`synchk.sh`), and reuses `template-cache.json` when possible.

### Compile and run
```
./run-answers.sh eval-<timestamp> <cores>
# or if native ocamlopt is unavailable:
./run-answers-bytecode.sh eval-<timestamp> <cores>
```
Per-problem compile/run logs live next to each generated `.ml` inside `eval-<timestamp>/`.
Edit `timeouts.json` to adjust limits.

### Collect verdicts
```
python3 collect-verdicts.py eval-<timestamp> results.csv
```
`*.judge.txt` files record `OK`, `FAIL`, `CHEATING`, or `ERROR`.


## Maintainers
- Balaji Rao 
- Juneyoung Lee — contact via GitHub [@aqjune](https://github.com/aqjune)


## Acknowledgements
[Juneyoung Lee](https://github.com/aqjune) was instrumental in building this project. He guided the process of collecting, tagging, and building the problem set, and later helped with the obfuscation. The main assessment logic has been appropriated for the Pass@K NTP attempts.

## Repository contents (quick reference)
- `problems/`, `problems.json` — generated corpus (may be absent until you run `collect-problems.py`).
- `prompt-basic.txt` — baseline functional-correctness prompt.
- `timeouts.json` — per-category timeout overrides for the runners.
- `run-obfuscation.py`, `strip.sh`, `synchk.sh` — helpers for obfuscation, stripping, and syntax checking.
- `template-cache.json` — cache of template-to-problem mappings; safe to delete when layouts change.
- `runs/` — created for when you execute workflows that emit answers/evaluations (not versioned).
