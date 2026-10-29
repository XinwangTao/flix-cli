package com.relaydesk.app

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONObject

/** Local state: devices, pairing trust, groups and chat history. */
class Store(context: Context) :
    SQLiteOpenHelper(context, "relaydesk.db", null, 5) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""CREATE TABLE devices(
            fingerprint TEXT PRIMARY KEY, alias TEXT, device_model TEXT,
            port INTEGER, version INTEGER, ip TEXT, os TEXT, seen_at INTEGER)""")
        db.execSQL("""CREATE TABLE pairs(
            fingerprint TEXT PRIMARY KEY, code TEXT, paired_at INTEGER)""")
        db.execSQL("""CREATE TABLE messages(
            id TEXT PRIMARY KEY, from_device TEXT, to_device TEXT, type INTEGER,
            time INTEGER, group_id TEXT DEFAULT '', text_content TEXT,
            file_name TEXT, file_size INTEGER DEFAULT 0, file_path TEXT,
            status INTEGER NOT NULL DEFAULT 0)""")
        db.execSQL("""CREATE TABLE incoming(
            resource_id TEXT PRIMARY KEY, bubble_id TEXT, sender TEXT,
            name TEXT, size INTEGER, path TEXT, waiting INTEGER DEFAULT 1,
            group_id TEXT DEFAULT '')""")
        db.execSQL("""CREATE TABLE groups(
            id TEXT PRIMARY KEY, name TEXT, created_at INTEGER,
            rev INTEGER NOT NULL DEFAULT 0, left_at INTEGER NOT NULL DEFAULT 0,
            pinned INTEGER NOT NULL DEFAULT 0)""")
        db.execSQL("""CREATE TABLE group_members(
            group_id TEXT, fingerprint TEXT, PRIMARY KEY(group_id, fingerprint))""")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) {
            // v1 databases predate delivery status; failed sends need a marker.
            db.execSQL("ALTER TABLE messages ADD COLUMN status INTEGER NOT NULL DEFAULT 0")
        }
        if (oldVersion < 3) {
            db.execSQL("""CREATE TABLE IF NOT EXISTS groups(
                id TEXT PRIMARY KEY, name TEXT, created_at INTEGER)""")
            db.execSQL("""CREATE TABLE IF NOT EXISTS group_members(
                group_id TEXT, fingerprint TEXT, PRIMARY KEY(group_id, fingerprint))""")
            db.execSQL("ALTER TABLE incoming ADD COLUMN group_id TEXT DEFAULT ''")
        }
        if (oldVersion < 4) {
            // Group metadata sync (v2C): rev orders snapshots, left_at marks
            // a group we were removed from (history stays readable).
            db.execSQL("ALTER TABLE groups ADD COLUMN rev INTEGER NOT NULL DEFAULT 0")
            db.execSQL("ALTER TABLE groups ADD COLUMN left_at INTEGER NOT NULL DEFAULT 0")
        }
        if (oldVersion < 5) {
            // Conversation pinning (v2D). Additive, so a re-run is harmless.
            db.execSQL("ALTER TABLE groups ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        }
    }

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
            "SELECT fingerprint, alias, device_model, port, version, ip, os, seen_at" +
            " FROM devices ORDER BY alias", null).use { c ->
            while (c.moveToNext()) {
                out.add(JSONObject().apply {
                    put("fingerprint", c.getString(0)); put("alias", c.getString(1) ?: "")
                    put("deviceModel", c.getString(2) ?: ""); put("port", c.getInt(3))
                    put("version", c.getInt(4)); put("ip", c.getString(5) ?: "")
                    put("os", c.getString(6) ?: ""); put("seenAt", c.getLong(7))
                })
            }
        }
        return out
    }

    fun device(fpPrefix: String): JSONObject? =
        devices().firstOrNull { it.optString("fingerprint").startsWith(fpPrefix) }

    fun deviceIp(fp: String): String? =
        device(fp)?.optString("ip")?.takeIf { it.isNotEmpty() }

    fun hasRecord(fp: String): Boolean =
        readableDatabase.rawQuery("SELECT 1 FROM devices WHERE fingerprint=?",
            arrayOf(fp)).use { it.moveToFirst() }

    fun hasHistory(fp: String): Boolean =
        readableDatabase.rawQuery(
            "SELECT 1 FROM messages WHERE from_device=? OR to_device=? LIMIT 1",
            arrayOf(fp, fp)).use { it.moveToFirst() }

    /**
     * The same machine re-created its identity (v2G): carry history, trust,
     * group membership onto the new fingerprint and drop the stale device row.
     * One transaction — a failure leaves the old identity untouched.
     */
    fun mergeIdentity(oldFp: String, newFp: String) {
        if (oldFp.isEmpty() || newFp.isEmpty() || oldFp == newFp) return
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.execSQL("UPDATE messages SET from_device=? WHERE from_device=?",
                arrayOf(newFp, oldFp))
            db.execSQL("UPDATE messages SET to_device=? WHERE to_device=?",
                arrayOf(newFp, oldFp))
            db.execSQL("INSERT OR IGNORE INTO pairs(fingerprint, code, paired_at)" +
                " SELECT ?, code, paired_at FROM pairs WHERE fingerprint=?",
                arrayOf(newFp, oldFp))
            db.execSQL("DELETE FROM pairs WHERE fingerprint=?", arrayOf(oldFp))
            db.execSQL("INSERT OR IGNORE INTO group_members(group_id, fingerprint)" +
                " SELECT group_id, ? FROM group_members WHERE fingerprint=?",
                arrayOf(newFp, oldFp))
            db.execSQL("DELETE FROM group_members WHERE fingerprint=?", arrayOf(oldFp))
            db.delete("devices", "fingerprint=?", arrayOf(oldFp))
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

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
                   fileSize: Long = 0, filePath: String? = null, status: Int = 0) {
        val v = ContentValues().apply {
            put("id", id); put("from_device", from); put("to_device", to)
            put("type", type); put("time", time); put("group_id", groupId)
            put("text_content", text); put("file_name", fileName)
            put("file_size", fileSize); put("file_path", filePath)
            put("status", status)
        }
        writableDatabase.insertWithOnConflict("messages", null, v,
            SQLiteDatabase.CONFLICT_REPLACE)
    }

    /** 0 = delivered/local, 2 = send failed (retryable from the chat). */
    fun setMessageStatus(id: String, status: Int) {
        writableDatabase.execSQL("UPDATE messages SET status=? WHERE id=?",
            arrayOf(status, id))
    }

    /** Local-only delete: the peer keeps their copy (v2D). */
    fun deleteMessage(id: String) {
        writableDatabase.delete("messages", "id=?", arrayOf(id))
    }

    fun deleteConversation(peer: String) {
        writableDatabase.delete("messages", "from_device=? OR to_device=?",
            arrayOf(peer, peer))
    }

    /** Dropping a group chat also drops the group, so it cannot come back empty. */
    fun deleteGroup(groupId: String) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.delete("messages", "group_id=?", arrayOf(groupId))
            db.delete("group_members", "group_id=?", arrayOf(groupId))
            db.delete("groups", "id=?", arrayOf(groupId))
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    private companion object {
        const val MESSAGE_COLUMNS = "id, from_device, to_device, type, time," +
            " text_content, file_name, file_size, file_path, status"

        /** LIKE wildcards in user input must match literally. */
        fun escapeLike(q: String): String =
            q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    }

    /** Newest last, matching what the bubble list expects. */
    private fun messageRows(sql: String, args: Array<String>): List<JSONObject> {
        val out = mutableListOf<JSONObject>()
        readableDatabase.rawQuery(sql, args).use { c ->
            while (c.moveToNext()) {
                out.add(JSONObject().apply {
                    put("id", c.getString(0)); put("from", c.getString(1))
                    put("to", c.getString(2)); put("type", c.getInt(3))
                    put("time", c.getLong(4))
                    if (!c.isNull(5)) put("text", c.getString(5))
                    if (!c.isNull(6)) put("fileName", c.getString(6))
                    put("fileSize", c.getLong(7))
                    if (!c.isNull(8)) put("filePath", c.getString(8))
                    put("status", c.getInt(9))
                })
            }
        }
        return out.reversed()
    }

    /** Latest messages for a peer conversation (either direction), newest last. */
    fun conversation(peer: String, limit: Int = 200): List<JSONObject> =
        messageRows(
            "SELECT $MESSAGE_COLUMNS FROM messages WHERE from_device=? OR to_device=?" +
            " ORDER BY time DESC LIMIT ?",
            arrayOf(peer, peer, limit.toString()))

    /** In-conversation search over text and file names (v2D), newest last. */
    fun searchConversation(peer: String, query: String, limit: Int = 200): List<JSONObject> {
        val like = "%${escapeLike(query)}%"
        return messageRows(
            "SELECT $MESSAGE_COLUMNS FROM messages WHERE (from_device=? OR to_device=?)" +
            " AND (text_content LIKE ? ESCAPE '\\' OR file_name LIKE ? ESCAPE '\\')" +
            " ORDER BY time DESC LIMIT ?",
            arrayOf(peer, peer, like, like, limit.toString()))
    }

    fun armIncoming(resourceId: String, bubbleId: String, sender: String,
                    name: String, size: Long, groupId: String = "") {
        val v = ContentValues().apply {
            put("resource_id", resourceId); put("bubble_id", bubbleId)
            put("sender", sender); put("name", name); put("size", size)
            put("group_id", groupId); put("waiting", 1)
        }
        writableDatabase.insertWithOnConflict("incoming", null, v,
            SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun incoming(resourceId: String): JSONObject? =
        readableDatabase.rawQuery(
            "SELECT bubble_id, sender, name, size, path, waiting, group_id" +
            " FROM incoming WHERE resource_id=?", arrayOf(resourceId)).use { c ->
            if (c.moveToFirst()) JSONObject().apply {
                put("bubbleId", c.getString(0)); put("sender", c.getString(1))
                put("name", c.getString(2)); put("size", c.getLong(3))
                if (!c.isNull(4)) put("path", c.getString(4))
                put("waiting", c.getInt(5)); put("groupId", c.getString(6) ?: "")
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

    fun unreadCount(peer: String, since: Long): Int =
        readableDatabase.rawQuery(
            "SELECT COUNT(*) FROM messages WHERE from_device=? AND time>?",
            arrayOf(peer, since.toString())).use { c ->
            if (c.moveToFirst()) c.getInt(0) else 0
        }

    /** Last bubble in either direction, for the conversation-list preview. */
    fun lastMessage(peer: String): JSONObject? =
        readableDatabase.rawQuery(
            "SELECT from_device, type, time, text_content, file_name FROM messages" +
            " WHERE from_device=? OR to_device=? ORDER BY time DESC LIMIT 1",
            arrayOf(peer, peer)).use { c ->
            if (!c.moveToFirst()) null else JSONObject().apply {
                put("from", c.getString(0)); put("type", c.getInt(1))
                put("time", c.getLong(2))
                if (!c.isNull(3)) put("text", c.getString(3))
                if (!c.isNull(4)) put("fileName", c.getString(4))
            }
        }

    // ---------- groups ----------

    /** rev 0 = placeholder for an unknown group; any real snapshot beats it. */
    fun upsertGroup(id: String, name: String,
                    createdAt: Long = System.currentTimeMillis(), rev: Long = 0L) {
        writableDatabase.execSQL(
            "INSERT INTO groups(id, name, created_at, rev) VALUES(?,?,?,?)" +
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            arrayOf<Any?>(id, name, createdAt, rev))
    }

    fun addGroupMember(groupId: String, fingerprint: String) {
        writableDatabase.execSQL(
            "INSERT OR IGNORE INTO group_members(group_id, fingerprint) VALUES(?,?)",
            arrayOf(groupId, fingerprint))
    }

    fun removeGroupMember(groupId: String, fingerprint: String) {
        writableDatabase.execSQL(
            "DELETE FROM group_members WHERE group_id=? AND fingerprint=?",
            arrayOf<Any?>(groupId, fingerprint))
    }

    fun groups(): List<JSONObject> {
        val out = mutableListOf<JSONObject>()
        readableDatabase.rawQuery(
            "SELECT g.id, g.name, g.created_at, g.rev, g.left_at, g.pinned," +
            " (SELECT COUNT(*) FROM group_members m WHERE m.group_id=g.id)" +
            " FROM groups g ORDER BY g.pinned DESC, g.created_at DESC", null).use { c ->
            while (c.moveToNext()) {
                out.add(JSONObject().apply {
                    put("id", c.getString(0)); put("name", c.getString(1))
                    put("createdAt", c.getLong(2)); put("rev", c.getLong(3))
                    put("leftAt", c.getLong(4)); put("pinned", c.getInt(5))
                    put("memberCount", c.getInt(6))
                })
            }
        }
        return out
    }

    fun setGroupPinned(id: String, pinned: Boolean) {
        writableDatabase.execSQL("UPDATE groups SET pinned=? WHERE id=?",
            arrayOf<Any?>(if (pinned) 1 else 0, id))
    }

    fun group(id: String): JSONObject? =
        groups().firstOrNull { it.optString("id") == id }

    fun groupMembers(groupId: String): List<String> {
        val out = mutableListOf<String>()
        readableDatabase.rawQuery(
            "SELECT fingerprint FROM group_members WHERE group_id=? ORDER BY fingerprint",
            arrayOf(groupId)).use { c ->
            while (c.moveToNext()) out.add(c.getString(0))
        }
        return out
    }

    fun groupRev(groupId: String): Long =
        readableDatabase.rawQuery("SELECT rev FROM groups WHERE id=?",
            arrayOf(groupId)).use { if (it.moveToFirst()) it.getLong(0) else 0L }

    /** Local authoritative write from the group-management UI (always wins). */
    fun writeGroup(id: String, name: String, members: List<String>, rev: Long) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.execSQL(
                "INSERT INTO groups(id, name, created_at, rev, left_at) VALUES(?,?,?,?,0)" +
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, rev=excluded.rev," +
                " left_at=0",
                arrayOf<Any?>(id, name, System.currentTimeMillis(), rev))
            db.execSQL("DELETE FROM group_members WHERE group_id=?", arrayOf<Any?>(id))
            for (fp in members) {
                db.execSQL("INSERT OR IGNORE INTO group_members(group_id, fingerprint)" +
                    " VALUES(?,?)", arrayOf<Any?>(id, fp))
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    /** Incoming snapshot: only a strictly newer rev overwrites name + members. */
    fun applyGroupSnapshot(id: String, name: String, members: List<String>, rev: Long): Boolean {
        val known = readableDatabase.rawQuery(
            "SELECT rev FROM groups WHERE id=?", arrayOf(id)).use {
            if (it.moveToFirst()) it.getLong(0) else -1L
        }
        if (known >= 0 && rev <= known) return false
        writeGroup(id, name, members, rev)
        return true
    }

    /** Removed from the group: keep the history, stop sending, drop the badge. */
    fun markGroupLeft(id: String, rev: Long) {
        writableDatabase.execSQL(
            "UPDATE groups SET left_at=?, rev=? WHERE id=?",
            arrayOf<Any?>(System.currentTimeMillis(), rev, id))
    }

    fun isGroupLeft(groupId: String): Boolean =
        readableDatabase.rawQuery("SELECT left_at FROM groups WHERE id=?",
            arrayOf(groupId)).use { it.moveToFirst() && it.getLong(0) > 0 }

    fun groupConversation(groupId: String, limit: Int = 200): List<JSONObject> =
        messageRows(
            "SELECT $MESSAGE_COLUMNS FROM messages WHERE group_id=? ORDER BY time DESC LIMIT ?",
            arrayOf(groupId, limit.toString()))

    fun searchGroupConversation(groupId: String, query: String,
                                limit: Int = 200): List<JSONObject> {
        val like = "%${escapeLike(query)}%"
        return messageRows(
            "SELECT $MESSAGE_COLUMNS FROM messages WHERE group_id=?" +
            " AND (text_content LIKE ? ESCAPE '\\' OR file_name LIKE ? ESCAPE '\\')" +
            " ORDER BY time DESC LIMIT ?",
            arrayOf(groupId, like, like, limit.toString()))
    }

    fun groupLastMessage(groupId: String): JSONObject? =
        readableDatabase.rawQuery(
            "SELECT from_device, type, time, text_content, file_name FROM messages" +
            " WHERE group_id=? ORDER BY time DESC LIMIT 1", arrayOf(groupId)).use { c ->
            if (!c.moveToFirst()) null else JSONObject().apply {
                put("from", c.getString(0)); put("type", c.getInt(1))
                put("time", c.getLong(2))
                if (!c.isNull(3)) put("text", c.getString(3))
                if (!c.isNull(4)) put("fileName", c.getString(4))
            }
        }

    /** Messages from others in a group newer than [since]. */
    fun groupUnread(groupId: String, me: String, since: Long): Int =
        readableDatabase.rawQuery(
            "SELECT COUNT(*) FROM messages WHERE group_id=? AND from_device<>? AND time>?",
            arrayOf(groupId, me, since.toString())).use { c ->
            if (c.moveToFirst()) c.getInt(0) else 0
        }
}
