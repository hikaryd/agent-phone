#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd "$(dirname "$0")" && pwd)
sdk_root=${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}
build_tools="$sdk_root/build-tools/34.0.0"
android_jar="$sdk_root/platforms/android-35/android.jar"
java_home=${PHONE_ADB_JAVA_HOME:-/opt/homebrew/opt/openjdk@17}
sign_dir=${PHONE_ADB_SIGN_DIR:-$HOME/.local/share/phone-adb-recovery}
keystore="$sign_dir/signing.p12"
password_file="$sign_dir/signing-password"
apk="$sign_dir/phone-adb-recovery.apk"
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT

mkdir -p "$sign_dir" "$build_dir/classes" "$build_dir/dex"
chmod 700 "$sign_dir"
if [[ ${PHONE_ADB_PROVISIONING_BUILD:-0} == 1 ]]; then
    python3 - "$source_dir/AndroidManifest.xml" "$build_dir/AndroidManifest.xml" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text()
needle = 'android:allowBackup="false"'
if source.count(needle) != 1:
    raise SystemExit("unexpected Android manifest")
Path(sys.argv[2]).write_text(source.replace(needle, needle + '\n        android:debuggable="true"'))
PY
else
    cp "$source_dir/AndroidManifest.xml" "$build_dir/AndroidManifest.xml"
fi
if [[ ! -f "$password_file" ]]; then
    python3 - "$password_file" <<'PY'
import os
import secrets
import sys

fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write(secrets.token_urlsafe(36) + "\n")
PY
fi
if [[ ! -f "$keystore" ]]; then
    "$java_home/bin/keytool" -genkeypair -noprompt -alias phone-adb-recovery \
        -keyalg RSA -keysize 3072 -validity 3650 \
        -dname 'CN=Phone ADB Recovery' -storetype PKCS12 \
        -keystore "$keystore" -storepass:file "$password_file" \
        -keypass:file "$password_file" >/dev/null
    chmod 600 "$keystore"
fi

"$build_tools/aapt" package -f -M "$build_dir/AndroidManifest.xml" \
    -I "$android_jar" -F "$build_dir/unsigned.apk"
"$java_home/bin/javac" --release 17 -classpath "$android_jar" \
    -d "$build_dir/classes" "$source_dir/RecoverReceiver.java"
JAVA_HOME="$java_home" "$build_tools/d8" --lib "$android_jar" \
    --output "$build_dir/dex" "$build_dir/classes/dev/agentphone/adbrecovery/RecoverReceiver.class"
cp "$build_dir/dex/classes.dex" "$build_dir/classes.dex"
(cd "$build_dir" && "$build_tools/aapt" add unsigned.apk classes.dex >/dev/null)
"$build_tools/zipalign" -f 4 "$build_dir/unsigned.apk" "$build_dir/aligned.apk"
JAVA_HOME="$java_home" "$build_tools/apksigner" sign \
    --ks "$keystore" --ks-key-alias phone-adb-recovery \
    --ks-pass "file:$password_file" \
    --out "$apk" "$build_dir/aligned.apk"
JAVA_HOME="$java_home" "$build_tools/apksigner" verify "$apk"
printf '%s\n' "$apk"
