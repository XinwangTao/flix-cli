package com.relaydesk.app

import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * In-process bridge between the foreground service and the Activity.
 * The service owns discovery, the channel server and notifications; the
 * Activity only observes events and asks for actions. No events are queued:
 * a closed Activity simply misses them and re-reads the store on resume.
 */
object Runtime {
    @Volatile var service: RelayService? = null
    @Volatile var servicePort: Int = 0
    @Volatile var serviceRunning: Boolean = false
    @Volatile var foregroundPeer: String? = null
    @Volatile var foregroundGroup: String? = null

    private val listeners = CopyOnWriteArraySet<(String, Any?) -> Unit>()

    fun addListener(listener: (String, Any?) -> Unit) = listeners.add(listener)

    fun removeListener(listener: (String, Any?) -> Unit) = listeners.remove(listener)

    fun hasListener(): Boolean = listeners.isNotEmpty()

    fun emit(event: String, payload: Any? = null) {
        for (listener in listeners) runCatching { listener(event, payload) }
    }

    /** A pairing request that the visible Activity must answer within 6s. */
    class PairPrompt(val peer: String, val code: String) {
        private val latch = CountDownLatch(1)
        private val answered = AtomicBoolean(false)

        @Volatile private var accepted = false

        fun answer(ok: Boolean) {
            if (!answered.compareAndSet(false, true)) return
            accepted = ok
            latch.countDown()
        }

        val isAnswered: Boolean
            get() = answered.get()

        fun await(timeoutMs: Long): Boolean {
            if (!latch.await(timeoutMs, TimeUnit.MILLISECONDS)) return false
            return accepted
        }
    }

    @Volatile private var pendingPair: PairPrompt? = null

    /** How long a pairing request may wait while the Activity is not visible. */
    const val PAIR_BACKGROUND_TIMEOUT_MS = 25_000L

    /**
     * Set by RelayService: shows the notification prompt instead of rejecting
     * a request that arrives with the app backgrounded or the screen off.
     */
    @Volatile var backgroundPairPrompter: ((String, String, Long) -> Boolean)? = null

    /**
     * Blocks the HTTP handler briefly while the user decides.
     *
     * With no visible Activity the request used to fail instantly, which is
     * what "没法配对" looks like from the desktop: the phone silently answered
     * 301 pair Device Null while the user was staring at a dialog-free screen.
     * It now falls back to a heads-up notification with real accept/reject
     * actions (RelayService wires backgroundPairPrompter).
     */
    fun requestPair(peer: String, code: String, timeoutMs: Long = 6000): Boolean {
        if (!hasListener()) {
            return backgroundPairPrompter?.invoke(peer, code,
                PAIR_BACKGROUND_TIMEOUT_MS) ?: false
        }
        val prompt = PairPrompt(peer, code)
        pendingPair = prompt
        emit("pairRequest", prompt)
        val accepted = prompt.await(timeoutMs)
        if (pendingPair === prompt) pendingPair = null
        // The sender has already given up; retract any dialog the user can
        // no longer answer into a real pairing.
        if (!accepted && !prompt.isAnswered) emit("pairTimeout", prompt)
        return accepted
    }

    /** Blocking wait for the notification path; the caller shows the prompt. */
    fun awaitBackgroundPair(peer: String, code: String, timeoutMs: Long,
                            show: (PairPrompt) -> Unit): Boolean {
        val prompt = PairPrompt(peer, code)
        pendingPair = prompt
        runCatching { show(prompt) }
        val accepted = prompt.await(timeoutMs)
        if (pendingPair === prompt) pendingPair = null
        return accepted
    }

    fun answerPair(ok: Boolean) {
        pendingPair?.answer(ok)
        pendingPair = null
    }
}
