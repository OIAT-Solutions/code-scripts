#!/bin/sh
set -eu

APP_ROOT="/app"
STATE_ROOT="${STATE_ROOT:-/data}"
SEED_ROOT="${SEED_ROOT:-/seed}"
COMPANIES_ROOT="${OIAT_COMPANIES_DIR:-${STATE_ROOT}/code_scripts/companies}"

mkdir -p \
  "${STATE_ROOT}" \
  "${STATE_ROOT}/runtime" \
  "${STATE_ROOT}/code_scripts" \
  "${COMPANIES_ROOT}" \
  "${STATE_ROOT}/code_scripts/Uploaded" \
  "${STATE_ROOT}/code_scripts/uploads" \
  "${STATE_ROOT}/code_scripts/logs" \
  "${STATE_ROOT}/code_scripts/reports" \
  "${STATE_ROOT}/code_scripts/outputs"

touch "${STATE_ROOT}/db.sqlite3"
touch "${STATE_ROOT}/code_scripts/qbo_tokens.sqlite"

seed_file_if_empty() {
  dst="$1"
  src="$2"

  if [ -f "${src}" ] && [ -s "${src}" ] && [ ! -s "${dst}" ]; then
    cp "${src}" "${dst}"
  fi
}

dir_has_entries() {
  find "$1" -mindepth 1 -print -quit | grep -q .
}

seed_dir_if_empty() {
  dst="$1"
  src="$2"

  if [ -d "${src}" ] && ! dir_has_entries "${dst}"; then
    cp -a "${src}/." "${dst}/"
  fi
}

link_path() {
  src="$1"
  dst="$2"

  mkdir -p "$(dirname "${dst}")"
  rm -rf "${dst}"
  ln -s "${src}" "${dst}"
}

# Seed once, never replace an installed approval (even an intentionally empty file).
mkdir -p "${STATE_ROOT}/mappings/company_a"
if [ ! -e "${STATE_ROOT}/mappings/company_a/approved.csv" ]; then
  cp "${APP_ROOT}/templates/product_conversion_empty.csv" "${STATE_ROOT}/mappings/company_a/approved.csv"
fi
# Till sheet line -> QBO bank map for the Undeposited Funds deposits (uf_deposits). Seed once,
# never replace an edited copy. The service-account key goes in ${STATE_ROOT}/secrets/ (by hand).
if [ ! -e "${STATE_ROOT}/mappings/company_a/till_accounts.csv" ]; then
  cp "${APP_ROOT}/templates/till_accounts_company_a.csv" "${STATE_ROOT}/mappings/company_a/till_accounts.csv"
fi
mkdir -p "${STATE_ROOT}/secrets"
chmod 700 "${STATE_ROOT}/secrets" 2>/dev/null || true
export COMPANY_A_PRODUCT_CONVERSION_FILE="${COMPANY_A_PRODUCT_CONVERSION_FILE:-${STATE_ROOT}/mappings/company_a/approved.csv}"

seed_file_if_empty "${STATE_ROOT}/db.sqlite3" "${SEED_ROOT}/db.sqlite3"
seed_file_if_empty "${STATE_ROOT}/code_scripts/qbo_tokens.sqlite" "${SEED_ROOT}/code_scripts/qbo_tokens.sqlite"
seed_dir_if_empty "${COMPANIES_ROOT}" "${SEED_ROOT}/code_scripts/companies"
seed_dir_if_empty "${STATE_ROOT}/code_scripts/Uploaded" "${SEED_ROOT}/code_scripts/Uploaded"
seed_dir_if_empty "${STATE_ROOT}/code_scripts/uploads" "${SEED_ROOT}/code_scripts/uploads"
seed_dir_if_empty "${STATE_ROOT}/code_scripts/logs" "${SEED_ROOT}/code_scripts/logs"
seed_dir_if_empty "${STATE_ROOT}/code_scripts/reports" "${SEED_ROOT}/code_scripts/reports"
seed_dir_if_empty "${STATE_ROOT}/code_scripts/outputs" "${SEED_ROOT}/code_scripts/outputs"

mkdir -p \
  "${STATE_ROOT}/code_scripts/uploads/range_raw" \
  "${STATE_ROOT}/code_scripts/uploads/spill_raw" \
  "${STATE_ROOT}/code_scripts/logs/runs"

link_path "${STATE_ROOT}/db.sqlite3" "${APP_ROOT}/db.sqlite3"
link_path "${STATE_ROOT}/runtime" "${APP_ROOT}/runtime"
link_path "${STATE_ROOT}/code_scripts/qbo_tokens.sqlite" "${APP_ROOT}/code_scripts/qbo_tokens.sqlite"
link_path "${COMPANIES_ROOT}" "${APP_ROOT}/code_scripts/companies"
link_path "${STATE_ROOT}/code_scripts/Uploaded" "${APP_ROOT}/code_scripts/Uploaded"
link_path "${STATE_ROOT}/code_scripts/uploads" "${APP_ROOT}/code_scripts/uploads"
link_path "${STATE_ROOT}/code_scripts/logs" "${APP_ROOT}/code_scripts/logs"
link_path "${STATE_ROOT}/code_scripts/reports" "${APP_ROOT}/code_scripts/reports"
link_path "${STATE_ROOT}/code_scripts/outputs" "${APP_ROOT}/code_scripts/outputs"

exec "$@"
