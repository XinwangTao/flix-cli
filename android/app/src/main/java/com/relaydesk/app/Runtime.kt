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

    /**
     * Blocks the HTTP handler briefly while the user decides. With no visible
     * Activity this returns false immediately (default reject).
     */
    fun requestPair(peer: String, code: String, timeoutMs: Long = 6000): Boolean {
        if (!hasListener()) return false
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

    fun answerPair(ok: Boolean) {
        pendingPair?.answer(ok)
        pendingPair = null
    }
}
