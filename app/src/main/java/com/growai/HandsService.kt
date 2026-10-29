package com.growai

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.view.accessibility.AccessibilityEvent
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** 손: 접근성 서비스로 다른 앱(게임) 화면을 직접 터치. 지금 앞에 있는 앱 패키지도 알려줌 */
class HandsService : AccessibilityService() {
    companion object {
        @Volatile var me: HandsService? = null
        @Volatile var pkg = ""
    }

    override fun onServiceConnected() { me = this }
    override fun onDestroy() { me = null; super.onDestroy() }
    override fun onInterrupt() {}

    override fun onAccessibilityEvent(e: AccessibilityEvent?) {
        if (e?.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val p = e.packageName?.toString() ?: return
        if (p != "com.android.systemui" && !p.contains("inputmethod") && !p.contains("keyboard")) pkg = p
    }

    private fun gesture(path: Path, ms: Long) {
        val done = CountDownLatch(1)
        val stroke = GestureDescription.StrokeDescription(path, 0, ms.coerceIn(1, 10000))
        val cb = object : AccessibilityService.GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) = done.countDown()
            override fun onCancelled(g: GestureDescription?) = done.countDown()
        }
        if (dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), cb, null))
            done.await(ms + 2000, TimeUnit.MILLISECONDS)
    }

    fun tap(x: Float, y: Float) = gesture(Path().apply { moveTo(x, y) }, 50)
    fun swipe(x1: Float, y1: Float, x2: Float, y2: Float, ms: Long) =
        gesture(Path().apply { moveTo(x1, y1); lineTo(x2 + 0.1f, y2 + 0.1f) }, ms)

    fun key(k: String) {
        performGlobalAction(when (k.lowercase()) {
            "home", "3" -> AccessibilityService.GLOBAL_ACTION_HOME
            "recents", "187" -> AccessibilityService.GLOBAL_ACTION_RECENTS
            else -> AccessibilityService.GLOBAL_ACTION_BACK  // back, 4 등
        })
    }
}
