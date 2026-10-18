# Welcome Videos Before Greeny

The five supplied Cloudinary MP4s are registered in the backend, playing in the specified sequence:
**1, 3, 5, 4, 2**. They stream directly from Cloudinary URLs with zero server bandwidth load.
All five URLs return HTTP 200 with MP4 video codecs.

## Public Endpoints (also under `/v1`):

- `GET /media/intro-videos`: Returns the ordered playlist, playback settings, and completion events.
- `GET /media/intro-player.js`: Reusable browser player module with sequential playback, muted autoplay, inline video, skip button ("Meet Greeny"), stall handling, and auto-cleanup.
- `GET /media/demo`: Live browser player & demo page to test and verify the 5 videos in real time.

---

## 5 Cloudinary Video Details & Playback Order

| Order | ID | Title | URL |
| :---: | :--- | :--- | :--- |
| **1** | `kiosk-1` | Kiosk 1 | `https://res.cloudinary.com/dsebrpcyz/video/upload/v1792224653/kiosk_1_online-video-cutter.com_wi5tpz.mp4` |
| **2** | `kiosk-3` | Kiosk 3 | `https://res.cloudinary.com/dsebrpcyz/video/upload/v1792224653/kiosk_3_online-video-cutter.com_hwn3z9.mp4` |
| **3** | `kiosk-5` | Kiosk 5 | `https://res.cloudinary.com/dsebrpcyz/video/upload/v1792224652/kiosk_5_online-video-cutter.com_qcvtfd.mp4` |
| **4** | `kiosk-4` | Kiosk 4 | `https://res.cloudinary.com/dsebrpcyz/video/upload/v1792224652/kiosk_4_online-video-cutter.com_otg7cq.mp4` |
| **5** | `kiosk-2` | Kiosk 2 | `https://res.cloudinary.com/dsebrpcyz/video/upload/v1792224652/kiosk_2_online-video-cutter.com_u2avhj.mp4` |

---

## Frontend Integration Options

### Option A: Standalone Playback (Before WebSocket Connection)

In your kiosk entry point (e.g. presence detection or customer wake):

```javascript
import { playKioskIntro } from "https://YOUR_BACKEND_HOST/media/intro-player.js";

const introContainer = document.getElementById("intro-container");
const avatarContainer = document.getElementById("avatar-container");

// 1. Hide avatar, show intro container
avatarContainer.hidden = true;
introContainer.hidden = false;

// 2. Play all 5 videos sequentially
const outcome = await playKioskIntro(introContainer, {
    apiBase: "https://YOUR_BACKEND_HOST"
});

// outcome is: "intro_done" | "intro_skipped" | "intro_failed"
introContainer.hidden = true;
avatarContainer.hidden = false;

// 3. Connect WebSocket with auto_greet enabled
const ws = new WebSocket(`wss://YOUR_BACKEND_HOST/ws/${sessionId}?v=2&auto_greet=1`);
```

---

### Option B: WebSocket Orchestration (`intro=1`)

If you want the backend to manage the intro lifecycle over the WebSocket:

1. Connect with `intro=1`:
```text
wss://YOUR_BACKEND_HOST/ws/<session_id>?v=2&auto_greet=1&intro=1
```

2. Listen for `SHOW_INTRO_VIDEOS` action and send completion events:
```javascript
import { playIntroPlaylist } from "https://YOUR_BACKEND_HOST/media/intro-player.js";

const introController = new AbortController();
let playingIntro = false;

ws.addEventListener("message", async ({ data }) => {
    const frame = JSON.parse(data);

    if (frame.type === "action" && frame.data.action === "SHOW_INTRO_VIDEOS") {
        if (playingIntro) return;
        playingIntro = true;
        avatarContainer.hidden = true;
        introContainer.hidden = false;

        const event = await playIntroPlaylist(introContainer, frame.data, {
            signal: introController.signal
        });

        introContainer.hidden = true;
        playingIntro = false;

        // Send completion event back to backend:
        // 'intro_done', 'intro_skipped', or 'intro_failed'
        if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: event }));
        }
    } else if (frame.type === "action" && frame.data.action === "SHOW_GREETING") {
        avatarContainer.hidden = false;
        // Greeny starts talking and greeting the customer here
    }
});

ws.addEventListener("close", () => introController.abort());
```

---

### React Component Example (`GreenyIntroPlayer.jsx`)

```jsx
import React, { useEffect, useRef, useState } from 'react';
import { playKioskIntro } from './intro-player.js'; // or from backend URL

export function GreenyIntroPlayer({ apiBase, onComplete }) {
    const containerRef = useRef(null);
    const [status, setStatus] = useState('playing');

    useEffect(() => {
        const controller = new AbortController();
        if (containerRef.current) {
            playKioskIntro(containerRef.current, {
                apiBase,
                signal: controller.signal
            }).then(outcome => {
                setStatus(outcome);
                if (onComplete) onComplete(outcome);
            });
        }
        return () => controller.abort();
    }, [apiBase, onComplete]);

    return (
        <div 
            ref={containerRef} 
            style={{ width: '100%', height: '100%', position: 'absolute', inset: 0, zIndex: 50 }} 
        />
    );
}
```

---

## Behavior & Safeguards

- **Muted Autoplay:** All modern browsers block unmuted autoplay without prior click. The intro plays muted to guarantee it starts immediately when the customer approaches the kiosk.
- **Skip Button:** A prominent "Meet Greeny" button allows customers to skip directly to conversation anytime.
- **Stall & Failure Protection:** If any video fails to load or stalls for >15 seconds, it automatically advances to the next video. If all 5 fail, it gracefully resolves with `intro_failed` so Greeny is never blocked.
- **Chat Gating:** While `intro=1` is pending, the backend queues/gates conversation messages and disables silence-reset timers so the intro is never interrupted by premature timeout resets.
