"""Public startup media: these videos are already hosted on Cloudinary."""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse


# Preserve the supplied playlist order. No media download occurs per API request.
INTRO_VIDEOS = (
    ('kiosk-1', 'Kiosk 1', 'https://res.cloudinary.com/dsebrpcyz/video/upload/v1792224653/kiosk_1_online-video-cutter.com_wi5tpz.mp4'),
    ('kiosk-3', 'Kiosk 3', 'https://res.cloudinary.com/dsebrpcyz/video/upload/v1792224653/kiosk_3_online-video-cutter.com_hwn3z9.mp4'),
    ('kiosk-5', 'Kiosk 5', 'https://res.cloudinary.com/dsebrpcyz/video/upload/v1792224652/kiosk_5_online-video-cutter.com_qcvtfd.mp4'),
    ('kiosk-4', 'Kiosk 4', 'https://res.cloudinary.com/dsebrpcyz/video/upload/v1792224652/kiosk_4_online-video-cutter.com_otg7cq.mp4'),
    ('kiosk-2', 'Kiosk 2', 'https://res.cloudinary.com/dsebrpcyz/video/upload/v1792224652/kiosk_2_online-video-cutter.com_u2avhj.mp4'),
)


def get_intro_playlist() -> dict:
    return {
        'version': 1,
        'playback': {
            'autoplay': True,
            'muted': True,
            'plays_inline': True,
            'loop': False,
            'allow_skip': True,
            'preload': 'metadata',
        },
        'videos': [
            {
                'id': key,
                'title': title,
                'url': url,
                'type': 'video/mp4',
            }
            for key, title, url in INTRO_VIDEOS
        ],
        'completion_events': ['intro_done', 'intro_skipped', 'intro_failed'],
        'next_action': 'SHOW_GREETING',
    }


router = APIRouter(prefix='/media', tags=['media'])


@router.api_route('/intro-videos', methods=['GET', 'HEAD'])
def intro_videos():
    """Available before authentication so the welcome screen can preload videos."""
    return get_intro_playlist()


@router.api_route('/intro-player.js', methods=['GET', 'HEAD'])
def intro_player():
    player_path = Path(__file__).parent / 'static' / 'intro-player.js'
    return FileResponse(player_path, media_type='text/javascript')


@router.api_route('/demo', methods=['GET', 'HEAD'])
def intro_demo():
    demo_path = Path(__file__).parent / 'static' / 'demo.html'
    if demo_path.exists():
        return FileResponse(demo_path, media_type='text/html')
    return HTMLResponse("<h1>Intro player demo not found</h1>", status_code=404)
