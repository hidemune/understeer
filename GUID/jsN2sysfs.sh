#! /bin/bash

for n in /sys/class/input/js*; do
  echo "=== $(basename $n) ==="
  tgt=$(readlink -f "$n/device")
  echo "$tgt"
  cat "$tgt/name" 2>/dev/null || true
  for f in bustype vendor product; do
    printf "%s=" "$f"; cat "$tgt/id/$f" 2>/dev/null || echo
  done
  [ -f "$tgt/phys" ] && { printf "phys="; cat "$tgt/phys"; }
  eve=$(readlink -f $tgt/event*)
  { printf "event="; echo "$eve"; }
done
