#!/bin/bash
# Build drizz-android-twins:api34 once on the machine that runs Harbor (Linux with /dev/kvm). Every task's
# environment/android/Dockerfile starts FROM it. ABI=arm64-v8a builds an arm64 phone for an arm64 host.
set -euo pipefail
cd "$(dirname "$0")"
# APKS.sha256 is committed: the image build fails if any APK differs from the builds this package was validated with.
[ -n "${REHASH_APKS:-}" ] && (cd apks && sha256sum *.apk) > APKS.sha256
# BUILD_NETWORK=host builds on the host network, for hosts where DNS inside docker build fails.
docker build ${BUILD_NETWORK:+--network="$BUILD_NETWORK"} --build-arg ABI="${ABI:-x86_64}" -t "${IMAGE:-drizz-android-twins:api34}" .
