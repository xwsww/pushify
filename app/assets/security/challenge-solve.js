(function () {
    const reasons = {
        invalid_client: "This browser cannot complete the check. Try another browser.",
        rate_limited: "Too many attempts. Wait a minute and refresh.",
        challenge_expired: "This check expired. Refresh the page.",
        host_mismatch: "Session mismatch. Refresh the page.",
        invalid_proof: "Invalid challenge. Refresh the page.",
        session_changed: "Your network changed during the check. Refresh the page.",
        invalid_fingerprint: "Browser check failed. Disable extensions or try another browser.",
        too_fast: "Check still starting. Refresh the page.",
        too_slow: "Check took too long. Refresh the page.",
        invalid_pow: "Verification failed. Refresh the page.",
        crypto: "This browser does not support required security features.",
        fingerprint: "Browser check failed. Disable extensions or try another browser.",
        timeout: "Verification timed out. Refresh the page to try again.",
        session_required: "Session expired. Refresh the page.",
        session_expired: "Session expired. Refresh the page.",
        bot_detected: "Automated access detected. Access denied.",
        invalid_browser: "Browser verification failed. Use a standard browser.",
    };

    function sleep(ms) {
        return new Promise((r) => setTimeout(r, ms));
    }

    async function sha256Hex(text) {
        const data = new TextEncoder().encode(text);
        const hash = await crypto.subtle.digest("SHA-256", data);
        return Array.from(new Uint8Array(hash))
            .map((b) => b.toString(16).padStart(2, "0"))
            .join("");
    }

    function isBotDetected() {
        const nav = navigator;
        const win = window;

        if (nav.webdriver === true) return "webdriver";
        if (win.callPhantom || win._phantom) return "phantom";
        if (win.__nightmare) return "nightmare";
        if (win.Cypress) return "cypress";
        if (win.selenium || win.webdriver || win.__webdriver_script_fn) return "selenium";
        if (nav.userAgent && nav.userAgent.includes("HeadlessChrome")) return "headless";
        if (nav.userAgent && nav.userAgent.includes("Headless")) return "headless";

        return null;
    }

    function getBrowserFingerprint() {
        const nav = navigator;
        const scr = screen;
        const win = window;

        return {
            ua: nav.userAgent || "",
            platform: nav.platform || "",
            vendor: nav.vendor || "",
            language: nav.language || "",
            languages: (nav.languages || []).join(","),
            cookieEnabled: nav.cookieEnabled,
            onLine: nav.onLine,
            hardwareConcurrency: nav.hardwareConcurrency || 0,
            deviceMemory: nav.deviceMemory || 0,
            maxTouchPoints: nav.maxTouchPoints || 0,
            pdfViewerEnabled: nav.pdfViewerEnabled || false,
            screenWidth: scr.width || 0,
            screenHeight: scr.height || 0,
            screenColorDepth: scr.colorDepth || 0,
            screenPixelDepth: scr.pixelDepth || 0,
            devicePixelRatio: win.devicePixelRatio || 1,
            innerWidth: win.innerWidth || 0,
            innerHeight: win.innerHeight || 0,
            outerWidth: win.outerWidth || 0,
            outerHeight: win.outerHeight || 0,
            timezoneOffset: new Date().getTimezoneOffset(),
            plugins: (nav.plugins || []).length,
            mimeTypes: (nav.mimeTypes || []).length,
            doNotTrack: nav.doNotTrack || "",
            pdfViewer: nav.mimeTypes && nav.mimeTypes["application/pdf"] !== undefined,
        };
    }

    async function loadBoot() {
        const res = await fetch("/.pushify/challenge/boot", {
            method: "GET",
            credentials: "same-origin",
            cache: "no-store",
            headers: { Accept: "application/json" },
        });
        let data = null;
        try {
            data = await res.json();
        } catch {
            return { ok: false, reason: "invalid_client" };
        }
        if (!res.ok || !data || !data.ok) {
            return {
                ok: false,
                reason: (data && (data.error || data.reason)) || "session_expired",
            };
        }
        if (data.redirect) {
            return { ok: true, redirect: data.redirect };
        }
        if (!data.issue || !data.issue.id) {
            return { ok: false, reason: "session_expired" };
        }
        return {
            ok: true,
            next: data.next || "/",
            issue: data.issue,
            minMs: data.minMs || 1000,
            maxMs: data.maxMs || 120000,
        };
    }

    async function collectFingerprint(bind) {
        const canvas = document.createElement("canvas");
        canvas.width = 64;
        canvas.height = 64;
        const c = canvas.getContext("2d");
        c.textBaseline = "top";
        c.font = "14px Arial";
        c.fillStyle = "#f60";
        c.fillRect(0, 0, 64, 64);
        c.fillStyle = "#069";
        c.fillText("pushify", 2, 2);
        const canvasHash = await sha256Hex(canvas.toDataURL());

        const uaData = navigator.userAgentData;
        const langs = navigator.languages || [];
        const alTag = (
            (langs[0] && String(langs[0])) ||
            navigator.language ||
            ""
        ).toLowerCase();

        const browserFp = getBrowserFingerprint();

        return {
            w: navigator.webdriver === true,
            lg: navigator.language || "",
            alt: alTag,
            lgs: (navigator.languages || []).join(","),
            pl: (uaData && uaData.platform) || navigator.platform || navigator.userAgent || "",
            hc: navigator.hardwareConcurrency || 0,
            dm: navigator.deviceMemory || 0,
            tz: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
            sw: screen.width,
            sh: screen.height,
            cd: screen.colorDepth,
            pr: window.devicePixelRatio || 1,
            ch: canvasHash.slice(0, 16),
            ck: navigator.cookieEnabled,
            sft: bind,
            fp: browserFp,
            ts: Date.now(),
        };
    }

    function fingerprintOk(fp, issue) {
        if (!fp || fp.w) return false;
        if (!fp.lg) return false;
        if (!fp.ch || fp.ch.length < 8) return false;
        if (!fp.sft || fp.sft !== issue.bind) return false;
        return true;
    }

    async function solve(id, difficulty, deadlineMs) {
        const prefix = "0".repeat(difficulty);
        const deadline = Date.now() + deadlineMs;
        let counter = 0;
        while (Date.now() < deadline) {
            const digest = await sha256Hex(id + ":" + counter);
            if (digest.startsWith(prefix)) return counter;
            counter += 1;
            if ((counter & 127) === 0) {
                await new Promise((r) => setTimeout(r, 0));
            }
        }
        return null;
    }

    async function verify(verifyUrl, issue, counter, fp, elapsed) {
        const res = await fetch(verifyUrl, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
            },
            body: JSON.stringify({
                id: issue.id,
                counter,
                proof: issue.proof,
                fp,
                elapsed_ms: elapsed,
            }),
        });
        let result = null;
        try {
            result = await res.json();
        } catch {
            return { ok: false, reason: "invalid_client" };
        }
        if (!res.ok) {
            return {
                ok: false,
                reason: (result && result.reason) || "invalid_pow",
            };
        }
        return result && result.ok ? result : { ok: false, reason: "invalid_pow" };
    }

    function fail(reasonKey) {
        const desc = document.querySelector(".desc");
        const message = reasons[reasonKey] || reasons.invalid_pow;
        if (desc) {
            desc.textContent = message;
            desc.classList.add("is-error");
        }
        const main = document.querySelector(".content");
        if (main) main.setAttribute("aria-busy", "false");
    }

    async function run() {
        const main = document.querySelector(".content");
        const wallStart = Date.now();
        const minVisibleMs = 2000;

        try {
            const botCheck = isBotDetected();
            if (botCheck) {
                fail("bot_detected");
                return;
            }

            const boot = await loadBoot();
            if (!boot.ok) {
                fail(boot.reason || "session_expired");
                return;
            }
            if (boot.redirect) {
                window.location.replace(boot.redirect);
                return;
            }

            const nextPath = boot.next || "/";
            const issue = boot.issue;
            const minSolveMs = boot.minMs || 1000;
            const maxSolveMs = boot.maxMs || 120000;
            const verifyUrl = "/.pushify/challenge/verify?next=" + encodeURIComponent(nextPath);

            if (!issue.id || !issue.proof || !issue.bind) {
                fail("challenge_expired");
                return;
            }
            if (typeof crypto === "undefined" || !crypto.subtle) {
                fail("crypto");
                return;
            }

            const fp = await collectFingerprint(issue.bind);
            if (!fingerprintOk(fp, issue)) {
                fail("fingerprint");
                return;
            }

            const powStart = Date.now();
            const budget = Math.max(8000, maxSolveMs - (powStart - wallStart) - 3000);
            const counter = await solve(issue.id, issue.difficulty, budget);
            if (counter === null) {
                fail("timeout");
                return;
            }

            let elapsed = Date.now() - powStart;
            if (elapsed < minSolveMs) {
                await sleep(minSolveMs - elapsed);
                elapsed = Date.now() - powStart;
            }

            const issuedAt = issue.issued_at || wallStart;
            const challengeAge = Date.now() - issuedAt;
            if (challengeAge < minSolveMs) {
                await sleep(minSolveMs - challengeAge);
            }

            if (elapsed > maxSolveMs) {
                fail("too_slow");
                return;
            }

            const result = await verify(verifyUrl, issue, counter, fp, elapsed);
            if (!result.ok) {
                fail(result.reason || "invalid_pow");
                return;
            }

            const visibleFor = Date.now() - wallStart;
            if (visibleFor < minVisibleMs) {
                await sleep(minVisibleMs - visibleFor);
            }

            if (main) main.setAttribute("aria-busy", "false");
            window.location.replace(result.redirect || nextPath || "/");
        } catch (err) {
            fail("invalid_pow");
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", run, { once: true });
    } else {
        run();
    }
})();
