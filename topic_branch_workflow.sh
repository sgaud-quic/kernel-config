#!/bin/bash
set -e

# Usage check
if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <branch-name> <commit-sha>"
    exit 1
fi

if [ -d "kernel-topics" ]; then
  echo "kernel-topics directory exists, Do you want to use same?"
  echo "Do you want to delete it ? [Y/n] Delete by default.."
  read RES
  if [ "$RES" == "Y" -o "$RES" == "y" -o "$RES" == "" ]; then
    echo "Deleting kernel-topics directory..."
    rm -rf kernel-topics
    echo "Syncing kernel-topics again..."
    echo "Do you want to use https or ssh for git push ?"
    read RES
    if [ "$RES" == "ssh" -o "$RES" == "" ]; then
      echo "Using ssh.."
      git clone git@github.com:qualcomm-linux/kernel-topics.git
    else
      echo "Using https."
      git clone https://github.com/qualcomm-linux/kernel-topics.git
    fi
  else
    echo "Using existing kernel-topics directory..."
  fi
else
   echo "Do you want to use https or ssh for git push ?"
    read RES
    if [ "$RES" == "ssh" -o "$RES" == "" ]; then
      echo "Using ssh.."
      git clone git@github.com:qualcomm-linux/kernel-topics.git
    else
      echo "Using https."
      git clone https://github.com/qualcomm-linux/kernel-topics.git
    fi
fi

REMOTE_NAME="torvalds"
REMOTE_URL="https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"
BRANCH_NAME=$1
COMMIT_SHA=$2

#git clone https://github.com/qualcomm-linux/kernel-topics.git

echo "=== Checkout to topic branch ==="
cd kernel-topics
git checkout "$BRANCH_NAME" || exit 1
CURRVER=$(awk '/^VERSION =/ {v=$3} /^PATCHLEVEL =/ {p=$3} /^EXTRAVERSION =/ {e=$3} END {print v "." p e}' Makefile)
TAG_NAME="${BRANCH_NAME}-${CURRVER}-$(date +%Y%m%d)"

echo $TAG_NAME

echo "=== Tagging the branch ==="
git tag -a "$TAG_NAME" -m "$BRANCH_NAME Topic branch based on ${CURRVER}"
git push origin "$TAG_NAME" -f

echo "=== Resetting the branch ==="
echo "$REMOTE_NAME"
echo "$REMOTE_URL"
if ! git remote | grep -q "^${REMOTE_NAME}$"; then
  git remote add "$REMOTE_NAME" "$REMOTE_URL"
  echo "Remote '$REMOTE_NAME' added."
else
  echo "Remote '$REMOTE_NAME' already exists."
fi

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
