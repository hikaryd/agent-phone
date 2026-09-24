package dev.agentphone.adbrecovery;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.provider.Settings;
import android.util.Log;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;

public final class RecoverReceiver extends BroadcastReceiver {
    private static final String ACTION = "dev.agentphone.adbrecovery.RECOVER";
    private static final String TAG = "PhoneAdbRecovery";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || !ACTION.equals(intent.getAction())) {
            return;
        }
        String supplied = intent.getStringExtra("token");
        try {
            Path tokenFile = context.getFilesDir().toPath().resolve("command-token");
            byte[] expected = Files.readAllBytes(tokenFile);
            if (supplied == null || supplied.length() != 64 || expected.length != 65
                || !MessageDigest.isEqual(
                    (supplied + "\n").getBytes(StandardCharsets.US_ASCII), expected
                )) {
                Log.w(TAG, "Rejected unauthenticated request");
                return;
            }
        } catch (Exception error) {
            Log.e(TAG, "Recovery token unavailable", error);
            return;
        }
        try {
            if (Settings.Global.getInt(context.getContentResolver(), "adb_wifi_enabled", 0) == 0) {
                boolean changed = Settings.Global.putInt(
                    context.getContentResolver(), "adb_wifi_enabled", 1
                );
                Log.i(TAG, changed ? "Wireless debugging enable requested" : "Settings write failed");
            } else {
                Log.i(TAG, "Wireless debugging already enabled");
            }
        } catch (SecurityException error) {
            Log.e(TAG, "WRITE_SECURE_SETTINGS grant is missing", error);
        }
    }
}
