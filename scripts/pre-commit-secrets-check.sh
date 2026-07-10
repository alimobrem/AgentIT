#!/usr/bin/env bash
set -euo pipefail

# Pre-commit hook: block commits containing secrets or credentials.
# Install: cp scripts/pre-commit-secrets-check.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit

PATTERNS=(
    'AKIA[0-9A-Z]{16}'
    '-----BEGIN (RSA |EC )?PRIVATE KEY-----'
    'ghp_[a-zA-Z0-9]{36}'
    'gho_[a-zA-Z0-9]{36}'
    'glpat-[a-zA-Z0-9\-]{20,}'
    'sk-[a-zA-Z0-9]{20,}'
)

EXCLUDE_PATTERNS='test_|re\.compile|PATTERNS|hunter2|EXAMPLE|sk-1234567890|changeme|placeholder'

COMBINED=$(IFS='|'; echo "${PATTERNS[*]}")

STAGED=$(git diff --cached --name-only --diff-filter=ACM)
if [ -z "$STAGED" ]; then
    exit 0
fi

FOUND=0
while IFS= read -r file; do
    [[ "$file" == *.pyc ]] && continue
    [[ "$file" == *.db ]] && continue

    if git diff --cached -- "$file" | grep -inE "$COMBINED" | grep -ivE "$EXCLUDE_PATTERNS" > /dev/null 2>&1; then
        echo "BLOCKED: Potential secret found in staged changes for: $file"
        git diff --cached -- "$file" | grep -inE "$COMBINED" | grep -ivE "$EXCLUDE_PATTERNS" | head -3
        echo ""
        FOUND=1
    fi
done <<< "$STAGED"

if [ "$FOUND" -eq 1 ]; then
    echo "Commit blocked. Remove secrets before committing."
    echo "To bypass (NOT recommended): git commit --no-verify"
    exit 1
fi
