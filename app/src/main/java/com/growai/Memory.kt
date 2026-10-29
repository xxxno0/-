package com.growai

import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONArray
import org.json.JSONObject

/** 하나의 행동 = 설명 + 입력 목록 (예: "tap:0.5:0.8") */
data class Act(val desc: String, val does: List<String>)

/** 기억: 게임별 경험·교훈·조작법을 폰 안 SQLite에 영구 저장. 매 스텝 바로 기록 */
class Memory(ctx: Context) : SQLiteOpenHelper(ctx, "growai_memory.db", null, 1) {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE exp(game TEXT, ts REAL, state TEXT, action TEXT, score REAL)")
        db.execSQL("CREATE TABLE lesson(game TEXT, ts REAL, problem TEXT, fix TEXT)")
        db.execSQL("CREATE TABLE game(name TEXT PRIMARY KEY, steps INTEGER DEFAULT 0, actions TEXT)")
        db.execSQL("CREATE INDEX exp_game ON exp(game, ts)")
    }
    override fun onUpgrade(db: SQLiteDatabase, o: Int, n: Int) {}

    private val db get() = writableDatabase
    private fun now() = System.currentTimeMillis() / 1000.0
    private fun <T> q(sql: String, vararg a: String, f: (Cursor) -> T): List<T> =
        db.rawQuery(sql, arrayOf(*a)).use { c -> val out = mutableListOf<T>(); while (c.moveToNext()) out.add(f(c)); out }
    private fun words(t: String) = Regex("\\w{2,}").findAll(t.lowercase()).map { it.value }.toSet()
    private fun ensureGame(g: String) = db.execSQL("INSERT OR IGNORE INTO game(name) VALUES(?)", arrayOf(g))

    fun addExp(g: String, state: String, action: String, score: Double) {
        db.execSQL("INSERT INTO exp VALUES(?,?,?,?,?)", arrayOf(g, now(), state, action, score))
        ensureGame(g); db.execSQL("UPDATE game SET steps=steps+1 WHERE name=?", arrayOf(g))
    }

    fun addLesson(g: String, problem: String, fix: String) =
        db.execSQL("INSERT INTO lesson VALUES(?,?,?,?)", arrayOf(g, now(), problem, fix))

    /** 지금 상황과 단어가 많이 겹치는 교훈 + 잘 먹혔던 행동 */
    fun recall(g: String, state: String, k: Int = 3): String {
        val w = words(state)
        fun <T> top(rows: List<Pair<String, T>>) = rows.sortedByDescending { (w intersect words(it.first)).size }.take(k)
        val lessons = top(q("SELECT problem,fix FROM lesson WHERE game=? ORDER BY ts DESC LIMIT 300", g) { it.getString(0) to it.getString(1) })
        val good = top(q("SELECT state,action FROM exp WHERE game=? AND score>0.7 ORDER BY ts DESC LIMIT 500", g) { it.getString(0) to it.getString(1) })
        return (lessons.map { "[교훈] ${it.second}" } + good.map { "[성공 행동] 비슷한 상황에서 '${it.second}'" }).joinToString("\n")
    }

    fun recent(g: String, n: Int = 8) =
        q("SELECT action,score FROM exp WHERE game=? ORDER BY ts DESC LIMIT $n", g) { "${it.getString(0)} → 진전 ${(it.getDouble(1) * 100).toInt()}%" }
            .reversed().joinToString("\n")

    fun recentSeq(g: String, n: Int) =
        q("SELECT action,score FROM exp WHERE game=? ORDER BY ts DESC LIMIT $n", g) { it.getString(0) to it.getDouble(1) }.reversed()

    fun rates(g: String) = q("SELECT action,AVG(score) FROM exp WHERE game=? GROUP BY action", g) { it.getString(0) to it.getDouble(1) }.toMap()
    fun counts(g: String) = q("SELECT action,COUNT(*) FROM exp WHERE game=? GROUP BY action", g) { it.getString(0) to it.getInt(1) }.toMap()
    fun totalXp(g: String) = q("SELECT COALESCE(SUM(score),0) FROM exp WHERE game=?", g) { it.getDouble(0) }.first()
    fun stats(g: String): Pair<Int, Int> =
        (q("SELECT steps FROM game WHERE name=?", g) { it.getInt(0) }.firstOrNull() ?: 0) to
        q("SELECT COUNT(*) FROM lesson WHERE game=?", g) { it.getInt(0) }.first()

    fun getActions(g: String): MutableMap<String, Act>? {
        val s = q("SELECT actions FROM game WHERE name=?", g) { it.getString(0) ?: "" }.firstOrNull()
        return if (s.isNullOrBlank()) null else fromJson(JSONObject(s))
    }

    fun saveActions(g: String, acts: Map<String, Act>) {
        ensureGame(g); db.execSQL("UPDATE game SET actions=? WHERE name=?", arrayOf(toJson(acts).toString(), g))
    }

    companion object {
        val SPEC = Regex("^(tap|click|swipe|hold|key|wait):[\\w.:]+$")
        fun toJson(a: Map<String, Act>) = JSONObject().also { o ->
            a.forEach { (k, v) -> o.put(k, JSONObject().put("desc", v.desc).put("do", JSONArray(v.does))) }
        }
        /** AI가 만든 JSON도 이걸로 검증: 허용된 입력 형식만 통과 */
        fun fromJson(o: JSONObject): MutableMap<String, Act> {
            val out = mutableMapOf<String, Act>()
            for (k in o.keys()) {
                val v = o.optJSONObject(k) ?: continue
                val arr = v.optJSONArray("do") ?: continue
                val does = (0 until arr.length()).map { arr.optString(it) }
                if (does.isNotEmpty() && does.all { SPEC.matches(it) }) out[k] = Act(v.optString("desc", k), does)
            }
            return out
        }
    }
}
