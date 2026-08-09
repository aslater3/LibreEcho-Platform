#!/usr/bin/env bash
set -euo pipefail

usage() { printf '%s\n' 'usage: build_wireless_regdb.sh --archive FILE --output DIR'; }
ARCHIVE=
OUTPUT=
while (($#)); do
  case "$1" in
    --archive) shift; (($#)) || { usage >&2; exit 2; }; ARCHIVE=$1 ;;
    --output) shift; (($#)) || { usage >&2; exit 2; }; OUTPUT=$1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$ARCHIVE" && -n "$OUTPUT" ]] || { usage >&2; exit 2; }
[[ -f "$ARCHIVE" && ! -L "$ARCHIVE" ]] || { printf 'ERROR: unsafe wireless-regdb archive\n' >&2; exit 1; }
[[ ! -e "$OUTPUT" ]] || { printf 'ERROR: refusing to overwrite wireless-regdb output: %s\n' "$OUTPUT" >&2; exit 1; }
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
LOCK="$SCRIPT_DIR/SOURCE.lock"
expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_sha256"])' "$LOCK")
actual=$(sha256sum "$ARCHIVE" | awk '{print $1}')
[[ "$actual" == "$expected" ]] || { printf 'ERROR: wireless-regdb source hash mismatch\n' >&2; exit 1; }
mkdir -p "$OUTPUT"
OUTPUT=$(cd -- "$OUTPUT" && pwd -P)
work=$(mktemp -d "${TMPDIR:-/tmp}/libreecho-wireless-regdb-build.XXXXXX")
trap 'rm -rf "$work"' EXIT
tar --no-same-owner --no-same-permissions -xJf "$ARCHIVE" -C "$work" --strip-components=1
for name in regulatory.db regulatory.db.p7s; do
  [[ -f "$work/$name" && ! -L "$work/$name" ]] || { printf 'ERROR: source archive lacks %s\n' "$name" >&2; exit 1; }
  expected_output=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["outputs"][sys.argv[2]]["sha256"])' "$LOCK" "$name")
  expected_size=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["outputs"][sys.argv[2]]["size"])' "$LOCK" "$name")
  actual_output=$(sha256sum "$work/$name" | awk '{print $1}')
  actual_size=$(stat -c %s "$work/$name")
  [[ "$actual_output" == "$expected_output" && "$actual_size" == "$expected_size" ]] || {
    printf 'ERROR: generated %s identity mismatch\n' "$name" >&2; exit 1;
  }
  install -m 0644 "$work/$name" "$OUTPUT/$name"
done
python3 - "$OUTPUT/wireless-regdb-source.json" "$LOCK" "$OUTPUT" <<'PY'
import json, pathlib, sys
out, lock, root = sys.argv[1:]
data = json.loads(pathlib.Path(lock).read_text())
data["outputs"] = {
    name: {"sha256": __import__("hashlib").sha256((pathlib.Path(root) / name).read_bytes()).hexdigest(),
           "size": (pathlib.Path(root) / name).stat().st_size}
    for name in data["outputs"]
}
pathlib.Path(out).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
PY
printf 'regulatory_db_sha256=%s\nregulatory_db_signature_sha256=%s\n' "$(sha256sum "$OUTPUT/regulatory.db" | awk '{print $1}')" "$(sha256sum "$OUTPUT/regulatory.db.p7s" | awk '{print $1}')"
