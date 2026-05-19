(function () {
    const canvas = document.getElementById("blobs");
    if (!canvas) return;
    const ctx = canvas.getContext("2d", { alpha: true });
    let W = 0;
    let H = 0;
    let rafId = 0;
    let running = true;
    let lastFrame = 0;
    const FRAME_MS = 1000 / 24;

    function resize() {
        W = window.innerWidth;
        H = window.innerHeight;
        canvas.width = W;
        canvas.height = H;
    }
    resize();
    window.addEventListener("resize", resize, { passive: true });

    function getScale() {
        const minDim = Math.min(W, H);
        if (minDim < 400) return 0.45;
        if (minDim < 600) return 0.6;
        if (minDim < 900) return 0.8;
        return 1;
    }

    const blobDefs = [
        { cx: 0.38, cy: 0.42, rx: 180, ry: 170, color: "rgba(157,126,255,0.45)", phase: 0, sx: 0.0004, sy: 0.00055, ax: 80, ay: 50 },
        { cx: 0.62, cy: 0.4, rx: 160, ry: 155, color: "rgba(65,200,220,0.4)", phase: 2.1, sx: 0.00045, sy: 0.0005, ax: 70, ay: 60 },
        { cx: 0.5, cy: 0.58, rx: 150, ry: 140, color: "rgba(249,170,80,0.32)", phase: 4.0, sx: 0.0005, sy: 0.00042, ax: 65, ay: 55 },
    ];

    function drawBlob(b, t, s) {
        const x = W * b.cx + Math.sin(t * b.sx * 6.28 + b.phase) * b.ax * s;
        const y = H * b.cy + Math.cos(t * b.sy * 6.28 + b.phase + 1.2) * b.ay * s;
        const rx = b.rx * s;
        const ry = b.ry * s;
        ctx.fillStyle = b.color;
        ctx.beginPath();
        ctx.ellipse(x, y, Math.max(rx, 1), Math.max(ry, 1), 0, 0, Math.PI * 2);
        ctx.fill();
    }

    function paintFrame(ts) {
        ctx.clearRect(0, 0, W, H);
        const s = getScale();
        for (const b of blobDefs) drawBlob(b, ts, s);
    }

    paintFrame(performance.now());
    document.body.classList.add("blobs-ready");

    function frame(ts) {
        if (!running) return;
        if (ts - lastFrame < FRAME_MS) {
            rafId = requestAnimationFrame(frame);
            return;
        }
        lastFrame = ts;
        paintFrame(ts);
        rafId = requestAnimationFrame(frame);
    }

    document.addEventListener("visibilitychange", () => {
        if (document.hidden) {
            running = false;
            cancelAnimationFrame(rafId);
        } else {
            running = true;
            lastFrame = 0;
            paintFrame(performance.now());
            rafId = requestAnimationFrame(frame);
        }
    });

    rafId = requestAnimationFrame(frame);
})();
