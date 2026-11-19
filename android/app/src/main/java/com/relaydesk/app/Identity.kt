package com.relaydesk.app

import android.content.Context
import android.provider.Settings
import java.util.UUID

/** Stable identity shared by the Activity and the foreground service. */
object Identity {
    private const val PREFS = "relaydesk"
    private const val KEY_FINGERPRINT = "fingerprint"

    /**
     * Persisted, but no longer minted at random when the prefs are gone.
     *
     * Peers key pairing rows, conversation threads and the device list on the
     * fingerprint. A wiped state (clear data, restore, reinstall that lost the
     * backup) used to produce a fresh UUID, so the phone came back as a
     * stranger: the desktop still had the old row paired, the new one was not,
     * and messages landed in a thread nobody was reading. Deriving it from
     * ANDROID_ID (stable per app signing key + device + user) keeps one
     * identity for the life of the phone, and commit() makes the write survive
     * an immediate process death.
     */
    fun fingerprint(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        prefs.getString(KEY_FINGERPRINT, null)
            ?.takeIf { it.isNotBlank() }
            ?.let { return it }
        val minted = derivedFingerprint(context)
        prefs.edit().putString(KEY_FINGERPRINT, minted).commit()
        return minted
    }

    private fun derivedFingerprint(context: Context): String {
        val androidId = runCatching {
            Settings.Secure.getString(context.contentResolver,
                Settings.Secure.ANDROID_ID)
        }.getOrNull().orEmpty()
        val seed = "relaydesk:" + androidId.ifEmpty { context.packageName }
        return UUID.nameUUIDFromBytes(seed.toByteArray(Charsets.UTF_8)).toString()
    }

    fun alias(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return prefs.getString("alias", null)?.takeIf { it.isNotBlank() }
            ?: android.os.Build.MODEL.also {
                prefs.edit().putString("alias", it).commit()
            }
    }

    /** Renaming is a prefs write; peers pick it up on the next /ping_v2. */
    fun setAlias(context: Context, alias: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString("alias", alias).commit()
    }
}
