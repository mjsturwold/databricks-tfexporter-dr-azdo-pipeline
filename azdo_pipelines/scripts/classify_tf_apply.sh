#!/usr/bin/env bash
set -euo pipefail

LOG_FILE="${1:?usage: classify_tf_apply.sh LOG_FILE APPLY_STATUS}"
APPLY_STATUS="${2:?usage: classify_tf_apply.sh LOG_FILE APPLY_STATUS}"

if [ "$APPLY_STATUS" -eq 0 ]; then
  exit 0
fi

mapfile -t ERRORS < <(
  sed $'s/\033\[[0-9;]*m//g' "$LOG_FILE" |
    sed -n 's/^.*Error: //p'
)

if [ "${#ERRORS[@]}" -eq 0 ]; then
  echo "##vso[task.logissue type=error]Terraform apply failed without a classifiable error."
  exit "$APPLY_STATUS"
fi

for error in "${ERRORS[@]}"; do
  if [[ ! "$error" =~ ^cannot\ delete\ directory:\ Folder\ .+\ is\ protected$ ]]; then
    echo "##vso[task.logissue type=error]Terraform apply contained a non-tolerated error: $error"
    exit "$APPLY_STATUS"
  fi
done

echo "##vso[task.logissue type=warning]Terraform returned only Databricks protected-directory deletion errors. Treating those errors as non-fatal; review the apply log and resulting state."
exit 0
