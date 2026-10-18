// Greenie Kiosk Intro Video Player
// Sequential playback of Cloudinary welcome videos with muted autoplay, skip, and event dispatch.

export async function playKioskIntro(container, { apiBase = "", signal } = {}) {
    try {
        const url = `${apiBase.replace(/\/$/, "")}/media/intro-videos`;
        const response = await fetch(url, { signal });
        if (!response.ok) return "intro_failed";
        const playlist = await response.json();
        return await playIntroPlaylist(container, playlist, { signal });
    } catch {
        return signal?.aborted ? "intro_skipped" : "intro_failed";
    }
}

export function playIntroPlaylist(container, playlist, { signal, onProgress } = {}) {
    return new Promise(resolve => {
        if (!container || !playlist?.videos?.length) return resolve("intro_failed");
        if (signal?.aborted) return resolve("intro_skipped");

        const host = document.createElement("div");
        host.className = "greeny-intro-host";
        host.style.cssText = "position:relative;width:100%;height:100%;background:#000;overflow:hidden;";

        const video = document.createElement("video");
        video.className = "greeny-intro-video";
        video.muted = true; // Muted autoplay is allowed across all modern browsers without user gesture
        video.playsInline = true;
        video.autoplay = true;
        video.preload = "metadata";
        video.style.cssText = "display:block;width:100%;height:100%;object-fit:contain;background:#000;";
        video.setAttribute("aria-label", "GreenFibre welcome videos");

        // UI overlay with video indicator & skip button
        const overlay = document.createElement("div");
        overlay.style.cssText = "position:absolute;top:0;left:0;right:0;bottom:0;pointer-events:none;display:flex;flex-direction:column;justify-content:space-between;padding:24px;box-sizing:border-box;";

        // Top badges / indicators
        const topBar = document.createElement("div");
        topBar.style.cssText = "display:flex;justify-content:space-between;align-items:center;pointer-events:auto;";
        
        const badge = document.createElement("span");
        badge.style.cssText = "background:rgba(0,0,0,0.6);color:#fff;padding:6px 14px;border-radius:20px;font-size:13px;font-family:system-ui,sans-serif;backdrop-filter:blur(4px);border:1px solid rgba(255,255,255,0.2);";
        badge.textContent = "Green Fibre Welcome";

        const counter = document.createElement("span");
        counter.style.cssText = "background:rgba(0,0,0,0.6);color:#86efac;padding:6px 14px;border-radius:20px;font-size:13px;font-family:system-ui,sans-serif;backdrop-filter:blur(4px);border:1px solid rgba(134,239,172,0.3);";
        counter.textContent = `1 / ${playlist.videos.length}`;

        topBar.append(badge, counter);

        // Bottom action bar
        const bottomBar = document.createElement("div");
        bottomBar.style.cssText = "display:flex;justify-content:flex-end;pointer-events:auto;";

        const skip = document.createElement("button");
        skip.type = "button";
        skip.textContent = "Meet Greeny →";
        skip.style.cssText = "padding:12px 24px;border:0;border-radius:30px;background:#15803d;color:#fff;font-size:15px;font-weight:600;font-family:system-ui,sans-serif;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,0.4);transition:all 0.2s ease;";
        skip.onmouseover = () => { skip.style.background = "#16a34a"; skip.style.transform = "scale(1.04)"; };
        skip.onmouseout = () => { skip.style.background = "#15803d"; skip.style.transform = "scale(1)"; };

        bottomBar.append(skip);
        overlay.append(topBar, bottomBar);
        host.append(video, overlay);
        container.append(host);

        let index = -1, failed = 0, finished = false, generation = 0;
        let stallTimer, limitTimer;
        const clearTimers = () => { clearTimeout(stallTimer); clearTimeout(limitTimer); };

        const finish = event => {
            if (finished) return;
            finished = true;
            clearTimers();
            signal?.removeEventListener("abort", abort);
            video.onended = video.onerror = video.onwaiting = video.onstalled = video.onplaying = null;
            try {
                video.pause();
                video.removeAttribute("src");
                video.load();
            } catch {}
            try {
                host.remove();
            } catch {}
            resolve(event);
        };

        const abort = () => finish("intro_skipped");

        const next = (didFail = false, expected = generation) => {
            if (finished || expected !== generation) return;
            clearTimers();
            if (didFail) failed++;
            index++;
            generation++;
            if (index >= playlist.videos.length) {
                finish(failed === playlist.videos.length ? "intro_failed" : "intro_done");
                return;
            }

            const current = generation;
            const currentVideo = playlist.videos[index];
            counter.textContent = `${index + 1} / ${playlist.videos.length} • ${currentVideo.title || ""}`;

            if (typeof onProgress === "function") {
                try {
                    onProgress({ index, total: playlist.videos.length, video: currentVideo });
                } catch {}
            }

            video.onended = () => next(false, current);
            video.onerror = () => next(true, current);

            const wait = () => {
                clearTimeout(stallTimer);
                stallTimer = setTimeout(() => next(true, current), 15000);
            };
            video.onwaiting = video.onstalled = wait;
            video.onplaying = () => clearTimeout(stallTimer);

            // Maximum duration fallback per video
            wait();
            limitTimer = setTimeout(() => next(true, current), 180000);

            video.src = currentVideo.url;
            try {
                video.load();
                video.play().catch(() => next(true, current));
            } catch {
                next(true, current);
            }
        };

        skip.onclick = abort;
        signal?.addEventListener("abort", abort, { once: true });
        next();
    });
}

// Attach to window for standard browser script tags
if (typeof window !== "undefined") {
    window.GreenyIntro = { playIntroPlaylist, playKioskIntro };
}
