package com.relaydesk.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Boot-time update prompt. OEMs (OPPO/OnePlus) may block auto-start unless the
 * user allows it; the app-launch check in MainActivity is the fallback.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        runCatching {
            context.startForegroundService(
                Intent(context, RelayService::class.java)
                    .setAction(RelayService.ACTION_CHECK_UPDATE))
        }
    }
}
