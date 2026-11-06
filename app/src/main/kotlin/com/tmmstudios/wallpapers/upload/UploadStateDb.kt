package com.tmmstudios.wallpapers.upload

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/** Persists handled media IDs, content hashes, and uploader metadata. */
class UploadStateDb(context: Context) :
    SQLiteOpenHelper(context.applicationContext, "upload-state.db", null, 1) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE done (id INTEGER PRIMARY KEY)")
        db.execSQL("CREATE TABLE hashes (h TEXT PRIMARY KEY)")
        db.execSQL("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit

    fun doneIds(): HashSet<Long> = HashSet<Long>().also { ids ->
        readableDatabase.rawQuery("SELECT id FROM done", null).use { cursor ->
            while (cursor.moveToNext()) ids.add(cursor.getLong(0))
        }
    }

    fun markDone(id: Long) {
        writableDatabase.execSQL("INSERT OR IGNORE INTO done(id) VALUES (?)", arrayOf<Any>(id))
    }

    fun markAllDone(ids: List<Long>) {
        writableDatabase.beginTransaction()
        try {
            ids.forEach { id ->
                writableDatabase.execSQL("INSERT OR IGNORE INTO done(id) VALUES (?)", arrayOf<Any>(id))
            }
            writableDatabase.setTransactionSuccessful()
        } finally {
            writableDatabase.endTransaction()
        }
    }

    fun hasHash(hash: String): Boolean =
        readableDatabase.rawQuery("SELECT 1 FROM hashes WHERE h = ?", arrayOf(hash)).use {
            it.moveToFirst()
        }

    fun addHash(hash: String) {
        writableDatabase.execSQL("INSERT OR IGNORE INTO hashes(h) VALUES (?)", arrayOf<Any>(hash))
    }

    fun getMeta(key: String): String? =
        readableDatabase.rawQuery("SELECT v FROM meta WHERE k = ?", arrayOf(key)).use {
            if (it.moveToFirst()) it.getString(0) else null
        }

    fun setMeta(key: String, value: String) {
        writableDatabase.execSQL(
            "INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)",
            arrayOf<Any>(key, value),
        )
    }
}
