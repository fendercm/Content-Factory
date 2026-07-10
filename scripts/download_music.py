"""
Скачивает бесплатные фоновые треки с Pixabay CDN и сохраняет в assets/music/.
Запуск: py scripts/download_music.py
"""

import os
import sys
import requests

BASE_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "music")

TRACKS = [
    # calm — несколько вариантов на случай 403
    {
        "urls": [
            "https://cdn.pixabay.com/download/audio/2022/05/27/audio_1808fbf07a.mp3",
            "https://cdn.pixabay.com/download/audio/2023/06/07/audio_8db5b11a8c.mp3",
            "https://cdn.pixabay.com/download/audio/2021/08/09/audio_88447e769f.mp3",
        ],
        "dest": os.path.join(BASE_DIR, "calm", "calm_ambient_01.mp3"),
    },
    # upbeat
    {
        "urls": [
            "https://cdn.pixabay.com/download/audio/2022/10/25/audio_946e698a29.mp3",
            "https://cdn.pixabay.com/download/audio/2023/01/09/audio_9e7bb2396f.mp3",
            "https://cdn.pixabay.com/download/audio/2022/08/02/audio_884fe92c21.mp3",
        ],
        "dest": os.path.join(BASE_DIR, "upbeat", "upbeat_energetic_01.mp3"),
    },
    # dramatic
    {
        "urls": [
            "https://cdn.pixabay.com/download/audio/2022/11/22/audio_febc508520.mp3",
            "https://cdn.pixabay.com/download/audio/2023/02/28/audio_5aaf7f9736.mp3",
            "https://cdn.pixabay.com/download/audio/2022/09/13/audio_5db3a8d4dc.mp3",
        ],
        "dest": os.path.join(BASE_DIR, "dramatic", "dramatic_cinematic_01.mp3"),
    },
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://pixabay.com/",
    "Accept": "audio/webm,audio/ogg,audio/wav,audio/*;q=0.9,*/*;q=0.8",
}


def download_one(url: str, dest: str) -> bool:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30, stream=True)
        resp.raise_for_status()
        data = b"".join(resp.iter_content(chunk_size=8192))
        if len(data) < 10_000:  # меньше 10 KB — скорее всего не аудио
            return False
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception:
        if os.path.exists(dest):
            os.remove(dest)
        return False


def download(track: dict) -> bool:
    dest = track["dest"]
    filename = os.path.basename(dest)
    print(f"  {filename}...", end=" ", flush=True)

    for url in track["urls"]:
        if download_one(url, dest):
            size_kb = os.path.getsize(dest) // 1024
            print(f"OK ({size_kb} KB)")
            return True

    # Fallback: скопировать уже скачанный трек из любого mood-каталога
    for mood in ("calm", "upbeat", "dramatic"):
        mood_dir = os.path.join(BASE_DIR, mood)
        for fname in os.listdir(mood_dir) if os.path.isdir(mood_dir) else []:
            src = os.path.join(mood_dir, fname)
            if os.path.getsize(src) > 10_000:
                import shutil
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                print(f"FALLBACK (copied from {fname})")
                return True

    print("ОШИБКА: все URL недоступны, нет fallback-трека")
    return False


def main() -> None:
    print(f"Скачиваю {len(TRACKS)} треков в assets/music/...\n")
    ok = 0
    for track in TRACKS:
        if download(track):
            ok += 1
    print(f"\nГотово: {ok}/{len(TRACKS)} треков скачано.")
    if ok == 0:
        print("Нет ни одного трека. Проверьте соединение.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
