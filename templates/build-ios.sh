#!/usr/bin/env bash
# MonkeyTester build-local: iOS build script.
#
# Commit this file as .monkeytester/build-ios.sh in your app repo.
# build-local runs it with bash from the root of a clean checkout of the branch
# being built, on the plugin's Mac (Xcode, and the signing identity in the
# login keychain of the user the plugin runs as):
#
#   MT_PLATFORM    ios
#   MT_OUTPUT      where this script must write the signed .ipa
#   MT_BRANCH      the branch being built
#   MT_CASES       comma-separated test case names (empty: every case)
#   MT_REQUEST_ID  the viewer's build id
#
# Exit 0 with a non-empty file at $MT_OUTPUT = built. Anything else is a build
# error: the plugin reports the output's last "error:" lines (xcodebuild's
# "error: ..." lines, e.g. signing) and attaches the whole output as the build
# log, and the iPhones show every test as Unavailable. Android is still built
# and tested.
#
# Replace for your app:
#   - the project (-project iosApp/iosApp.xcodeproj; use -workspace for a
#     CocoaPods/SPM workspace) and the scheme (iosApp),
#   - iosApp/ExportOptions.plist (method "debugging" or "release-testing" with
#     your team ID).
#
# The .ipa must install on the test iPhones: the provisioning profile must
# include each test iPhone's UDID. With -allowProvisioningUpdates and automatic
# signing, Xcode adds registered devices to the profile itself; register a new
# iPhone in the developer account (or open the project once in Xcode with the
# phone connected) before its first build.
set -euo pipefail

: "${MT_OUTPUT:?MT_OUTPUT is not set (this script is run by build-local)}"

out=build/monkeytester
mkdir -p "$out"

# BUILD: archive, then export a signed .ipa. Replace project, scheme and
# ExportOptions.plist with your app's.
xcodebuild archive \
  -project iosApp/iosApp.xcodeproj \
  -scheme iosApp \
  -configuration Debug \
  -destination "generic/platform=iOS" \
  -archivePath "$out/app.xcarchive" \
  -allowProvisioningUpdates

xcodebuild -exportArchive \
  -archivePath "$out/app.xcarchive" \
  -exportOptionsPlist iosApp/ExportOptions.plist \
  -exportPath "$out/export" \
  -allowProvisioningUpdates

ipa=$(find "$out/export" -name '*.ipa' | head -1)
if [ -z "$ipa" ]; then
  echo "error: xcodebuild exported no .ipa" >&2
  exit 1
fi
cp "$ipa" "$MT_OUTPUT"
