#!/usr/bin/env bash
# MonkeyTester build-local: Android build script.
#
# Commit this file as .monkeytester/build-android.sh in your app repo.
# build-local runs it with bash from the root of a clean checkout of the branch
# being built, on the plugin's own host (a Mac, or Linux with the Android SDK):
#
#   VT_PLATFORM    android
#   VT_OUTPUT      where this script must write the .apk
#   VT_BRANCH      the branch being built
#   VT_CASES       comma-separated test case names (empty: every case)
#   VT_REQUEST_ID  the viewer's build id
#
# Exit 0 with a non-empty file at $VT_OUTPUT = built. Anything else is a build
# error: the plugin reports the output's last "error:" lines and attaches the
# whole output as the build log, and the Android devices show every test as
# Unavailable.
#
# Replace for your app:
#   - the Gradle task (assembleDebug) if your app module or variant differs,
#   - the APK path (app/build/outputs/apk/debug/app-debug.apk).
set -euo pipefail

: "${VT_OUTPUT:?VT_OUTPUT is not set (this script is run by build-local)}"

# The Android SDK: $ANDROID_HOME / $ANDROID_SDK_ROOT, else the usual places on
# macOS and Linux. Gradle reads it from local.properties.
sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
if [ -z "$sdk" ]; then
  if [ -d "$HOME/Library/Android/sdk" ]; then
    sdk="$HOME/Library/Android/sdk"
  else
    sdk="$HOME/Android/Sdk"
  fi
fi
echo "sdk.dir=$sdk" > local.properties

# BUILD: a debug APK. Replace with your app's build.
./gradlew assembleDebug --no-daemon

apk=app/build/outputs/apk/debug/app-debug.apk
if [ ! -s "$apk" ]; then
  echo "error: Gradle produced no APK at $apk" >&2
  exit 1
fi
cp "$apk" "$VT_OUTPUT"
