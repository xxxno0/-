package com.growai

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.DisplayMetrics
import android.view.WindowManager
import java.io.ByteArrayOutputStream
import kotlin.concurrent.thread

/** 두뇌가 사는 집 + 눈: 화면 녹화(MediaProjection)로 게임 화면을 보고, 생명 루프를 백그라운드에서 돌림 */
class BrainService : Service() {
    companion object {
        @Volatile var running = false
        const val STOP = "com.growai.STOP"
    }

    private var proj: MediaProjection? = null
    private var vd: VirtualDisplay? = null
    private var reader: ImageReader? = null
    @Volatile var w = 0; @Volatile var h = 0   // 실제 화면 크기(터치 좌표용)
    private var dpi = 0

    override fun onBind(i: Intent?): android.os.IBinder? = null

    override fun onStartCommand(i: Intent?, flags: Int, id: Int): Int {
        if (i?.action == STOP || i == null) { running = false; stopSelf(); return START_NOT_STICKY }
        if (running) return START_NOT_STICKY
        startForegroundNow()  // 안드로이드 14: 화면 녹화 시작 전에 포그라운드 먼저
        val data: Intent = (if (Build.VERSION.SDK_INT >= 33) i.getParcelableExtra("data", Intent::class.java)
                            else @Suppress("DEPRECATION") i.getParcelableExtra<Intent>("data")) ?: return stopNow()
        val mpm = getSystemService(MediaProjectionManager::class.java)
        proj = mpm.getMediaProjection(i.getIntExtra("code", 0), data) ?: return stopNow()
        proj!!.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() { running = false; Bus.log("👁 화면 보기 권한이 끝났어요") }
        }, Handler(Looper.getMainLooper()))
        dpi = resources.displayMetrics.densityDpi
        realSize().let { (rw, rh) -> w = rw; h = rh }
        reader = ImageReader.newInstance(w / 2, h / 2, PixelFormat.RGBA_8888, 2)
        vd = proj!!.createVirtualDisplay("GrowAI-eyes", w / 2, h / 2, dpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader!!.surface, null, null)
        running = true
        Bus.log("🌱 깨어났어요. 게임을 열어주세요!")
        thread(name = "growai-brain") {
            try { Brain(this, Config(this), Memory(this)).live() } finally { stopSelf() }
        }
        return START_NOT_STICKY
    }

    private fun stopNow(): Int { Bus.log("⚠ 화면 보기 권한을 못 받았어요"); stopSelf(); return START_NOT_STICKY }

    private fun realSize(): Pair<Int, Int> {
        val wm = getSystemService(WindowManager::class.java)
        return if (Build.VERSION.SDK_INT >= 30) wm.maximumWindowMetrics.bounds.let { it.width() to it.height() }
        else DisplayMetrics().also { @Suppress("DEPRECATION") wm.defaultDisplay.getRealMetrics(it) }.let { it.widthPixels to it.heightPixels }
    }

    /** 가로 게임으로 바뀌면 눈(캡처 크기)도 같이 회전 */
    @Synchronized private fun followRotation() {
        val (rw, rh) = realSize()
        if (rw == w && rh == h) return
        w = rw; h = rh
        val old = reader
        reader = ImageReader.newInstance(w / 2, h / 2, PixelFormat.RGBA_8888, 2)
        vd?.resize(w / 2, h / 2, dpi); vd?.surface = reader!!.surface
        old?.close()
        Thread.sleep(300)
    }

    /** 지금 화면 한 장 (절반 해상도 비트맵) */
    @Synchronized fun bitmap(): Bitmap {
        followRotation()
        val r = reader ?: throw IllegalStateException("눈이 감겨 있어요")
        repeat(30) {
            val img = r.acquireLatestImage()
            if (img != null) img.use {
                val p = it.planes[0]
                val bw = p.rowStride / p.pixelStride
                val full = Bitmap.createBitmap(bw, it.height, Bitmap.Config.ARGB_8888)
                full.copyPixelsFromBuffer(p.buffer)
                return if (bw == it.width) full else Bitmap.createBitmap(full, 0, 0, it.width, it.height).also { full.recycle() }
            }
            Thread.sleep(50)
        }
        throw IllegalStateException("화면을 못 봤어요 (캡처를 막는 게임일 수 있음)")
    }

    /** AI에게 보낼 JPEG (긴 변 1024px) */
    fun jpeg(bmp: Bitmap = bitmap()): ByteArray {
        val s = 1024f / maxOf(bmp.width, bmp.height)
        val small = if (s < 1f) Bitmap.createScaledBitmap(bmp, (bmp.width * s).toInt(), (bmp.height * s).toInt(), true) else bmp
        return ByteArrayOutputStream().also { small.compress(Bitmap.CompressFormat.JPEG, 80, it) }.toByteArray()
    }

    private fun startForegroundNow() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel("growai", "GrowAI", NotificationManager.IMPORTANCE_LOW))
        val stopPi = PendingIntent.getService(this, 1, Intent(this, BrainService::class.java).setAction(STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val openPi = PendingIntent.getActivity(this, 2, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val n = Notification.Builder(this, "growai")
            .setContentTitle("GrowAI 살아있음 🌱").setContentText("게임하며 성장 중 · 정지는 아래 버튼")
            .setSmallIcon(android.R.drawable.ic_media_play).setOngoing(true).setContentIntent(openPi)
            .addAction(Notification.Action.Builder(null, "■ 정지", stopPi).build()).build()
        if (Build.VERSION.SDK_INT >= 29) startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
        else startForeground(1, n)
    }

    override fun onDestroy() {
        running = false
        vd?.release(); reader?.close(); proj?.stop()
        Bus.log("■ 잠들었어요 (기억은 저장됨)"); Bus.status("잠자는 중 💤")
        super.onDestroy()
    }
}
