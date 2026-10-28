package com.relaydesk.app

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.MulticastSocket
import org.json.JSONObject

/**
 * UDP discovery on 224.0.0.168:8891, ported from flixcli discovery.py:
 * bind :8891 (discovery port regardless of our TCP channel port), join the
 * group, burst announces (multicast + limited broadcast) 11 times, parse
 * double-encoded deviceModal datagrams. Requires an acquired MulticastLock.
 */
class Discovery(
    private val fingerprint: String,
    private val alias: String,
    private val channelPort: Int,
    private val onDevice: (JSONObject) -> Unit,
) {
    @Volatile private var rxSocket: MulticastSocket? = null
    @Volatile private var rxThread: Thread? = null
    @Volatile private var running = false

    fun start(): Boolean = try {
        val sock = MulticastSocket(Wire.DEVICE_PORT)
        sock.reuseAddress = true
        sock.joinGroup(InetAddress.getByName(Wire.MULTICAST_GROUP))
        rxSocket = sock
        running = true
        rxThread = Thread({
            val buf = ByteArray(64 * 1024)
            while (running) {
                try {
                    val packet = DatagramPacket(buf, buf.size)
                    sock.receive(packet)
                    val src = packet.address?.hostAddress ?: continue
                    Wire.parseAnnounce(packet.data, src)?.let { dev ->
                        if (dev.optString("fingerprint") != fingerprint) onDevice(dev)
                    }
                } catch (_: Exception) {
                    if (!running) break
                }
            }
        }, "relaydesk-discovery").apply { isDaemon = true; start() }
        true
    } catch (_: Exception) {
        false
    }

    fun announce(times: Int = Wire.ANNOUNCE_BURST) {
        val payload = Wire.buildAnnounce(fingerprint, alias, channelPort)
            .toByteArray(Charsets.UTF_8)
        val targets = listOfNotNull(
            runCatching { InetSocketAddress(InetAddress.getByName(Wire.MULTICAST_GROUP),
                                             Wire.DEVICE_PORT) }.getOrNull(),
            runCatching { InetSocketAddress(InetAddress.getByName("255.255.255.255"),
                                             Wire.DEVICE_PORT) }.getOrNull(),
        )
        Thread({
            DatagramSocket().use { tx ->
                tx.broadcast = true
                repeat(times) {
                    for (dst in targets) {
                        runCatching {
                            tx.send(DatagramPacket(payload, payload.size, dst))
                        }
                    }
                    Thread.sleep(60)
                }
            }
        }, "relaydesk-announce").apply { isDaemon = true; start() }
    }

    fun stop() {
        running = false
        runCatching { rxSocket?.close() }
        rxSocket = null
        rxThread = null
    }
}
