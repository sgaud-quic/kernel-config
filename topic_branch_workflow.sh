#!/bin/bash
set -e

# Usage check
if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <branch-name> <commit-sha>"
    exit 1
fi

BRANCH_NAME=$1
COMMIT_SHA=$2

git clone https://github.com/qualcomm-linux/kernel-topics.git

echo "=== Checkout to topic branch ==="
cd kernel-topics
git checkout "$BRANCH_NAME" || exit 1
CURRVER=$(awk '/^VERSION =/ {v=$3} /^PATCHLEVEL =/ {p=$3} /^SUBLEVEL =/ {s=$3} /^EXTRAVERSION =/ {e=$3} END {print v "." p "." s e}' Makefile)
TAG_NAME="${BRANCH_NAME}-${CURRVER}-$(date +%Y%m%d)"

echo $TAG_NAME

echo "=== Tagging the branch ==="
git tag -a "$TAG_NAME" -m "$BRANCH_NAME Topic branch based on ${CURRVER}"
git push origin "$TAG_NAME"

echo "=== Resetting the branch ==="
git remote add torvalds https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git 2>/dev/null
git fetch torvalds
git checkout main
git branch -D "$BRANCH_NAME"
git checkout -b "$BRANCH_NAME" "$COMMIT_SHA"

echo "=== Force push ==="
git push origin "$BRANCH_NAME" -f

echo "=== Copying GitHub workflow ==="
cp -r ../kernel-config/.github/kernel_topic_workflow_template/.github/ .
git add -f .github/
git commit -s -m "Github workflow for Topic Branch"

echo "=== force push ==="
git push origin "$BRANCH_NAME" -f

