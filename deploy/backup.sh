#!/usr/bin/env bash
# Online backup of the creature's database (safe while the engine is running; WAL-aware).
# Run it from host cron, e.g. nightly:   15 4 * * *  /path/to/internet-cat/deploy/backup.sh
#   ./data/nit.db  ->  ./data/backups/nit-YYYY-MM-DD.db   (keeps the newest N copies)
# Needs the sqlite3 CLI on the host (sudo apt install sqlite3). Overrides: CAT_ROOT, CAT_DB,
# CAT_BACKUP_DIR, CAT_BACKUP_KEEP (default: backup_keep in config/settings.yaml, else 14).
set -euo pipefail

ROOT="${CAT_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
DB="${CAT_DB:-$ROOT/data/nit.db}"
DEST="${CAT_BACKUP_DIR:-$ROOT/data/backups}"
KEEP="${CAT_BACKUP_KEEP:-$(sed -n 's/^backup_keep: *\([0-9][0-9]*\).*/\1/p' "$ROOT/config/settings.yaml" 2>/dev/null | head -n1)}"
KEEP="${KEEP:-14}"

command -v sqlite3 >/dev/null || { echo "backup: sqlite3 is not installed (sudo apt install sqlite3)" >&2; exit 1; }
[ -f "$DB" ] || { echo "backup: no database at $DB" >&2; exit 1; }

mkdir -p "$DEST"
OUT="$DEST/nit-$(date +%F).db"
TMP="$OUT.partial"
rm -f "$TMP"
sqlite3 "$DB" ".backup '$TMP'"
[ "$(sqlite3 "$TMP" 'PRAGMA integrity_check;')" = "ok" ] || { rm -f "$TMP"; echo "backup: integrity check failed" >&2; exit 1; }
mv -f "$TMP" "$OUT"

# keep the newest $KEEP copies
ls -1t "$DEST"/nit-????-??-??.db 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f --
echo "backup: $OUT ($(ls -1 "$DEST"/nit-????-??-??.db | wc -l) kept)"
