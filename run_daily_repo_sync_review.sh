#!/usr/bin/env bash
# V10.2 daily repository synchronization and deterministic code review.
# Intended for a persistent Linux host. Never commits, pushes, stashes, resets,
# or overwrites a working tree. It only fast-forward synchronizes a clean main branch.
set -uo pipefail

export TZ="${TZ:-Asia/Hong_Kong}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REMOTE_NAME="${HKJC_REPO_REMOTE:-origin}"
BRANCH_NAME="${HKJC_REPO_BRANCH:-main}"
RUN_DATE="${RUN_DATE:-$(date +%F)}"
LOG_DIR="${HKJC_REPO_REVIEW_LOG_DIR:-$ROOT_DIR/archive/daily_repo_review_logs}"
REPORT_DIR="${HKJC_REPO_REVIEW_REPORT_DIR:-$ROOT_DIR/archive/daily_repo_review_reports}"
LOCK_FILE="${HKJC_REPO_REVIEW_LOCK_FILE:-$ROOT_DIR/runtime/daily_repo_sync_review.lock}"
ARCHIVE_LOCK_FILE="${HKJC_DAILY_LOCK_FILE:-$ROOT_DIR/runtime/daily_archive_and_backfill.lock}"
LOG_FILE="$LOG_DIR/${RUN_DATE}.log"
REPORT_FILE="$REPORT_DIR/${RUN_DATE}.md"

mkdir -p "$LOG_DIR" "$REPORT_DIR" "$(dirname "$LOCK_FILE")"
if ! command -v flock >/dev/null 2>&1; then
  echo "$(date '+%F %T %Z') ERROR: flock is required for safe repository review." >&2
  exit 2
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$(date '+%F %T %Z') INFO: daily repository review is already running; skipped." >> "$LOG_FILE"
  exit 0
fi
exec >> "$LOG_FILE" 2>&1

write_report() {
  cat > "$REPORT_FILE" <<EOF
# V10.2 每日儲存庫同步與程式審核

| 欄位 | 值 |
|---|---|
| 執行時間 | $(date '+%F %T %Z') |
| 工作目錄 | \\`$ROOT_DIR\\` |
| 分支 | \\`$BRANCH_NAME\\` |
| 遠端 | \\`$REMOTE_NAME\\` |
| 狀態 | $1 |

## Git 同步

$2

## 程式審核

$3

> 本工作只會在工作區乾淨且可 fast-forward 時同步。它不會自動 commit、push、stash、reset 或覆蓋本機修改。
EOF
}

printf '%s START daily_repo_sync_review root=%s branch=%s\n' "$(date '+%F %T %Z')" "$ROOT_DIR" "$BRANCH_NAME"
cd "$ROOT_DIR"

if [[ ! -d .git ]]; then
  write_report "FAILED" "找不到 Git 工作區，未執行同步。" "未執行。"
  exit 2
fi
if [[ "$(git branch --show-current)" != "$BRANCH_NAME" ]]; then
  write_report "BLOCKED" "目前分支不是 \\`$BRANCH_NAME\\`，未執行同步。" "未執行。"
  exit 0
fi
if [[ -n "$(git status --porcelain)" ]]; then
  write_report "BLOCKED" "偵測到未提交或未追蹤檔案，為保護工作內容而未同步。" "未執行；請先審核或提交工作區變更。"
  exit 0
fi

# Do not fetch/pull while the SQLite archive/backfill workflow is active.
exec 8>"$ARCHIVE_LOCK_FILE"
if ! flock -n 8; then
  write_report "BLOCKED" "每日歸檔／海外回刷正在執行，未同步以避免在資料管線運行中更新程式碼。" "未執行；下一日排程會重試。"
  exit 0
fi
flock -u 8

git fetch --prune "$REMOTE_NAME" "$BRANCH_NAME"
local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse "$REMOTE_NAME/$BRANCH_NAME")"
merge_base="$(git merge-base HEAD "$REMOTE_NAME/$BRANCH_NAME")"
sync_note=""
if [[ "$local_head" == "$remote_head" ]]; then
  sync_note="本機已與 \\`$REMOTE_NAME/$BRANCH_NAME\\` 同步（${local_head:0:12}）。"
elif [[ "$local_head" == "$merge_base" ]]; then
  git merge --ff-only "$REMOTE_NAME/$BRANCH_NAME"
  sync_note="已安全 fast-forward 至 ${remote_head:0:12}。"
elif [[ "$remote_head" == "$merge_base" ]]; then
  write_report "BLOCKED" "本機分支領先遠端（${local_head:0:12}），本工作不會自動 push。" "未執行；請人工檢閱本機提交。"
  exit 0
else
  write_report "BLOCKED" "本機與遠端分支已分歧，本工作不會 merge、rebase 或 reset。" "未執行；請人工解決分歧。"
  exit 0
fi

checks=()
failed=0
run_check() {
  local label="$1"
  shift
  if "$@"; then
    checks+=("- PASS：$label")
  else
    checks+=("- FAIL：$label")
    failed=1
  fi
}

run_check "已追蹤 Python 程式語法編譯" bash -c 'mapfile -t files < <(git ls-files "*.py"); ((${#files[@]})) && "${PYTHON_BIN:-python3}" -m py_compile "${files[@]}"'
run_check "Git 差異格式檢查" git diff --check HEAD
run_check "敏感字串路徑掃描" bash -c '
  hits=$(git grep -nIE "(BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|TELEGRAM_BOT_TOKEN[[:space:]]*=[^[:space:]]+)" -- ":!*.md" || true)
  if [[ -n "$hits" ]]; then
    printf "%s\\n" "$hits" | cut -d: -f1-2
    exit 1
  fi
'
run_check "S1/S2 特徵契約測試" "$PYTHON_BIN" verify_s1s2_feature_enrichment.py
run_check "海外 archive／覆盤契約測試" "$PYTHON_BIN" verify_overseas_archive_audit_guidance.py

printf -v review_text '%s\n' "${checks[@]}"
if [[ $failed -ne 0 ]]; then
  write_report "REVIEW_FAILED" "$sync_note" "$review_text"
  printf '%s END status=review_failed report=%s\n' "$(date '+%F %T %Z')" "$REPORT_FILE"
  exit 1
fi
write_report "OK" "$sync_note" "$review_text"
printf '%s END status=ok report=%s\n' "$(date '+%F %T %Z')" "$REPORT_FILE"
