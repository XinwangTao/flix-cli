package com.relaydesk.app

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONObject

/** Local state: devices, pairing trust and chat history. */
class Store(context: Context) :
    SQLiteOpenHelper(context, "relaydesk.db", null, 1) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""CREATE TABLE devices(
            fingerprint TEXT PRIMARY KEY, alias TEXT, device_model TEXT,
            port INTEGER, version INTEGER, ip TEXT, os TEXT, seen_at INTEGER)""")
        db.execSQL("""CREATE TABLE pairs(
            fingerprint TEXT PRIMARY KEY, code TEXT, paired_at INTEGER)""")
        db.execSQL("""CREATE TABLE messages(
            id TEXT PRIMARY KEY, from_device TEXT, to_device TEXT, type INTEGER,
            time INTEGER, group_id TEXT DEFAULT '', text_content TEXT,
            file_name TEXT, file_size INTEGER DEFAULT 0, file_path TEXT)""")
        db.execSQL("""CREATE TABLE incoming(
            resource_id TEXT PRIMARY KEY, bubble_id TEXT, sender TEXT,
            name TEXT, size INTEGER, path TEXT, waiting INTEGER DEFAULT 1)""")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit

    fun upsertDevice(fp: String, alias: String, model: String, port: Int,
                     version: Int, ip: String, os: String) {
        val v = ContentValues().apply {
            put("fingerprint", fp); put("alias", alias); put("device_model", model)
            put("port", port); put("version", version); put("ip", ip)
            put("os", os); put("seen_at", System.currentTimeMillis())
        }
        writableDatabase.insertWithOnConflict("devices", null, v,
            SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun devices(): List<JSONObject> {
        val out = mutableListOf<JSONObject>()
        readableDatabase.rawQuery(
            "SELECT fingerprint, alias, device_model, port, version, ip, os" +
            " FROM devices ORDER BY alias", null).use { c ->
            while (c.moveToNext()) {
                out.add(JSONObject().apply {
                    put("fingerprint", c.getString(0)); put("alias", c.getString(1) ?: "")
                    put("deviceModel", c.getString(2) ?: ""); put("port", c.getInt(3))
                    put("version", c.getInt(4)); put("ip", c.getString(5) ?: "")
                    put("os", c.getString(6) ?: "")
                })
            }
        }
        return out
    }

    fun device(fpPrefix: String): JSONObject? =
        devices().firstOrNull { it.optString("fingerprint").startsWith(fpPrefix) }

    fun deviceIp(fp: String): String? =
        device(fp)?.optString("ip")?.takeIf { it.isNotEmpty() }

    fun isPaired(fp: String): Boolean =
        readableDatabase.rawQuery(
            "SELECT 1 FROM pairs WHERE fingerprint=?", arrayOf(fp)).use { it.moveToFirst() }

    fun addPair(fp: String, code: String) {
        val v = ContentValues().apply {
            put("fingerprint", fp); put("code", code)
            put("paired_at", System.currentTimeMillis())
        }
        writableDatabase.insertWithOnConflict("pairs", null, v,
            SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun ownPairKey(prefs: android.content.SharedPreferences): String =
        prefs.getString("device_pair_key", null) ?: java.util.UUID.randomUUID().toString()
            .substring(0, 4).also { prefs.edit().putString("device_pair_key", it).apply() }

    fun addMessage(id: String, from: String, to: String, type: Int, time: Long,
                   groupId: String, text: String? = null, fileName: String? = null,
                   fileSize: Long = 0, filePath: String? = null) {
        val v = ContentValues().apply {
            put("id", id); put("from_device", from); put("to_device", to)
            put("type", type); put("time", time); put("group_id", groupId)
            put("text_content", text); put("file_name", fileName)
            put("file_size", fileSize); put("file_path", filePath)
        }
        writableDatabase.insertWithOnConflict("messages", null, v,
            SQLiteDatabase.CONFLICT_REPLACE)
    }

    /** Latest messages for a peer conversation (either direction), newest last. */
    fun conversation(peer: String, limit: Int = 200): List<JSONObject> {
        val out = mutableListOf<JSONObject>()
        readableDatabase.rawQuery(
            "SELECT id, from_device, to_device, type, time, text_content," +
            " file_name, file_size, file_path FROM messages" +
            " WHERE from_device=? OR to_device=? ORDER BY time DESC LIMIT ?",
            arrayOf(peer, peer, limit.toString())).use { c ->
            while (c.moveToNext()) {
                out.add(JSONObject().apply {
                    put("id", c.getString(0)); put("from", c.getString(1))
                    put("to", c.getString(2)); put("type", c.getInt(3))
                    put("time", c.getLong(4))
                    if (!c.isNull(5)) put("text", c.getString(5))
                    if (!c.isNull(6)) put("fileName", c.getString(6))
                    put("fileSize", c.getLong(7))
                    if (!c.isNull(8)) put("filePath", c.getString(8))
                })
            }
        }
        return out.reversed()
    }

    fun armIncoming(resourceId: String, bubbleId: String, sender: String,
                    name: String, size: Long) {
        val v = ContentValues().apply {
            put("resource_id", resourceId); put("bubble_id", bubbleId)
            put("sender", sender); put("name", name); put("size", size)
            put("waiting", 1)
        }
        writableDatabase.insertWithOnConflict("incoming", null, v,
            SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun incoming(resourceId: String): JSONObject? =
        readableDatabase.rawQuery(
            "SELECT bubble_id, sender, name, size, path, waiting FROM incoming" +
            " WHERE resource_id=?", arrayOf(resourceId)).use { c ->
            if (c.moveToFirst()) JSONObject().apply {
                put("bubbleId", c.getString(0)); put("sender", c.getString(1))
                put("name", c.getString(2)); put("size", c.getLong(3))
                if (!c.isNull(4)) put("path", c.getString(4))
                put("waiting", c.getInt(5))
            } else null
        }

    fun commitIncoming(resourceId: String, path: String) {
        writableDatabase.execSQL(
            "UPDATE incoming SET path=?, waiting=0 WHERE resource_id=?",
            arrayOf(path, resourceId))
    }

    /** Newest incoming bubble time from a peer (0 if none) — drives unread badges. */
    fun lastIncoming(peer: String): Long =
        readableDatabase.rawQuery(
            "SELECT COALESCE(MAX(time), 0) FROM messages WHERE from_device=?",
            arrayOf(peer)).use { c ->
            if (c.moveToFirst()) c.getLong(0) else 0L
        }
}
