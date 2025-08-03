#!/bin/bash
UBUNTU_URL="https://cdimage.ubuntu.com/ubuntu-server/noble/daily-preinstalled/current/noble-preinstalled-server-arm64.img.xz"
IMG_XZ_NAME="noble-preinstalled-server-arm64.img.xz"
IMG_NAME="noble-preinstalled-server-arm64.img"
ROOTFS_IMG="ubuntu.img"
WORKDIR=$(pwd)
MNT_DIR="$WORKDIR/mnt"

echo "Salendar : $WORKDIR"