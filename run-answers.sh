#!/bin/bash

if [ "$#" -ne 2 ]; then
  echo "run-answers.sh <dir> <num-cores>"
  exit 1
fi

export HOLLIGHT_DIR=`pwd`/hol-light
eval $(opam env --switch $HOLLIGHT_DIR --set-switch)

hol_sh_cmd=$HOLLIGHT_DIR/hol.sh
export hol_sh_cmd

compile_and_run() {
  # i is the absolute path to the .ml file
  i=$1

  echo "${i}: Compiling"

  OCAMLRUNPARAM=l=2000000000 \
  ocamlopt.byte -pp "$(${hol_sh_cmd} -pp)" -I "${HOLLIGHT_DIR}" -I +unix -c \
      hol_lib.cmxa $i -o ${i%.ml}.cmx -w -a 1>${i%.ml}.compile.log 2>&1 && \
  ocamlfind ocamlopt -package zarith,unix -linkpkg hol_lib.cmxa \
      -I "${HOLLIGHT_DIR}" ${i%.ml}.cmx \
      -o "${i%.ml}.exe" || {
    echo "Using native OCaml compiler failed! falling back to bytecode compiler..."
    ocamlc -pp "$(${hol_sh_cmd} -pp)" -I "${HOLLIGHT_DIR}" -I +unix -c \
        hol_lib.cma $i -o ${i%.ml}.cmo -w -a 1>${i%.ml}.compile.log 2>&1
    ocamlfind ocamlc -package zarith,unix -linkpkg hol_lib.cma \
        -I "${HOLLIGHT_DIR}" ${i%.ml}.cmo \
        -o "${i%.ml}.exe"
  }

  echo "${i}: Running"
  cd objfiles
  ${i%.ml}.exe > ${i%.ml}.run.outlog 2> ${i%.ml}.run.errlog
  cd ..
}

export -f compile_and_run


evaldir=`realpath "$1"`
num_cores=$2
find "$evaldir" -name "*.ml" -print0 | xargs -0 -I {} -P "$num_cores" bash -c 'compile_and_run "$@"' _ {}
