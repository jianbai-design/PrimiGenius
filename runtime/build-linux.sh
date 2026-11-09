#!/usr/bin/env bash
set -euo pipefail
# Run in a disposable Fedora 44 build container; /project is read-only source,
# /inputs contains the checksum-verified upstream archive, /out is the artifact dir.
work=$(mktemp -d /tmp/primigenius-build.XXXXXX)
trap 'rm -rf -- "$work"' EXIT
echo '227589056a0576f6d41205e5de1be03545d4e664fa2cf3aa6265c139eaf0b974  /inputs/podman-source.tar.gz' | sha256sum -c -
tar -xzf /inputs/podman-source.tar.gz -C "$work"
source_dir="$work/podman-6d48b6f12f793176f3f6bc808b5a440984c14eb2"
mkdir -p "$source_dir/cmd/primigenius-engine" /out
cp /project/engine/main.go "$source_dir/cmd/primigenius-engine/main.go"
cd "$source_dir"
export CGO_ENABLED=1 GOTOOLCHAIN=local
export GOPROXY=${GOPROXY:-https://proxy.golang.org,direct}
go build -mod=vendor -trimpath -tags 'seccomp exclude_graphdriver_btrfs' \
  -ldflags '-s -w -X main.sourceCommit=6d48b6f12f793176f3f6bc808b5a440984c14eb2' \
  -o /out/primigenius-engine ./cmd/primigenius-engine
go build -mod=vendor -trimpath -tags 'seccomp exclude_graphdriver_btrfs' \
  -ldflags '-s -w' \
  -o /out/podman-linux ./cmd/podman
cp -a /project/bridge "$work/bridge"
cd "$work/bridge"
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -mod=readonly -trimpath -ldflags '-s -w' -o /out/primigenius-bridge-linux .
CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -mod=readonly -trimpath -o /out/primigenius-bridge.exe .
cp go.sum /out/bridge.go.sum
cp "$source_dir/LICENSE" /out/LICENSE.libpod
go version > /out/build-toolchain.txt
rpm -qa | sort > /out/build-packages.txt
sha256sum /out/primigenius-* > /out/SHA256SUMS
