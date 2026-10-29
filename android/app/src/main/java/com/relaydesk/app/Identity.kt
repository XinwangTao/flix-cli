package com.relaydesk.app

import android.content.Context

/** Stable identity shared by the Activity and the foreground service. */
object Identity {
    private const val PREFS = "relaydesk"

    fun fingerprint(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return prefs.getString("fingerprint", null) ?: Wire.newId().also {
            prefs.edit().putString("fingerprint", it).apply()
        }
    }

    fun alias(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        return prefs.getString("alias", null) ?: android.os.Build.MODEL.also {
            prefs.edit().putString("alias", it).apply()
        }
    }

    /** Renaming is a prefs write; peers pick it up on the next /ping_v2. */
    fun setAlias(context: Context, alias: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString("alias", alias).apply()
    }
}
