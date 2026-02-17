#!/bin/bash

if [ "$#" -ne 1 ]; then
  echo "synchk.sh <.ml file>"
  exit 1
fi

export HOLLIGHT_DIR=`pwd`/hol-light
eval $(opam env --switch $HOLLIGHT_DIR --set-switch)

inp=$1

ocamlc -pp "`$HOLLIGHT_DIR/hol.sh -pp`" -I "$HOLLIGHT_DIR" -i $inp > /dev/null 2>/dev/null
