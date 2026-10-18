import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../app/static/intro-player.js", import.meta.url), "utf8");
const { playIntroPlaylist } = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));

function dom() {
    const elements = [];
    globalThis.document = {
        createElement(tag) {
            const element = {
                tag,
                style: {},
                children: [],
                removed: false,
                className: "",
                append(...items) { this.children.push(...items); },
                setAttribute() {},
                removeAttribute(name) { delete this[name]; },
                remove() { this.removed = true; },
                pause() {},
                load() {},
                play() { return Promise.resolve(); },
            };
            elements.push(element);
            return element;
        }
    };
    return {
        container: { append() {} },
        get video() { return elements.find(e => e.tag === "video"); },
        get skip() { return elements.find(e => e.tag === "button"); },
        get host() { return elements.find(e => e.tag === "div" && e.className === "greeny-intro-host"); }
    };
}

const playlist = {
    videos: [
        { id: "kiosk-1", title: "Kiosk 1", url: "https://res.cloudinary.com/dsebrpcyz/video/upload/v1792224653/kiosk_1_online-video-cutter.com_wi5tpz.mp4" },
        { id: "kiosk-3", title: "Kiosk 3", url: "https://res.cloudinary.com/dsebrpcyz/video/upload/v1792224653/kiosk_3_online-video-cutter.com_hwn3z9.mp4" }
    ]
};

test("plays the supplied sequence and completes before showing Greeny", async () => {
    const ui = dom();
    const result = playIntroPlaylist(ui.container, playlist);
    assert.equal(ui.video.src, playlist.videos[0].url);
    assert.equal(ui.video.muted, true);
    ui.video.onended();
    assert.equal(ui.video.src, playlist.videos[1].url);
    ui.video.onended();
    assert.equal(await result, "intro_done");
    assert.equal(ui.host.removed, true);
});

test("skip releases the intro and cleans up video playback", async () => {
    const ui = dom();
    const result = playIntroPlaylist(ui.container, playlist);
    ui.skip.onclick();
    assert.equal(await result, "intro_skipped");
    assert.equal(ui.video.src, undefined);
    assert.equal(ui.host.removed, true);
});

test("unavailable clips are skipped and total failure releases Greeny", async () => {
    const ui = dom();
    const result = playIntroPlaylist(ui.container, playlist);
    ui.video.onerror();
    assert.equal(ui.video.src, playlist.videos[1].url);
    ui.video.onerror();
    assert.equal(await result, "intro_failed");
});

test("unmount or session cancellation stops playback", async () => {
    const ui = dom();
    const controller = new AbortController();
    const result = playIntroPlaylist(ui.container, playlist, { signal: controller.signal });
    controller.abort();
    assert.equal(await result, "intro_skipped");
    assert.equal(ui.host.removed, true);
});
