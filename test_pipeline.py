from agents.script_writer import write_script
from agents.voice_agent import generate_voice
from agents.subtitle_agent import generate_subtitles
from agents.visual_agent import get_visuals
from agents.assembly_agent import assemble_video

topic = "5 привычек продуктивных людей"
platform = "tiktok"

print("1. Генерирую сценарий...")
script = write_script(topic, platform)
print(f"   Hook: {script['hook'][:60]}...")

print("\n2. Синтезирую голос...")
voice = generate_voice(script["full_script"], platform)
print(f"   Аудио: {voice['audio_path']}")

print("\n3. Генерирую субтитры...")
subs = generate_subtitles(voice["audio_path"], platform)
print(f"   Субтитры: {subs['subtitle_path']} ({subs['segment_count']} чанков)")

print("\n4. Подбираю визуалы...")
visuals = get_visuals(topic, platform, num_clips=4)
print(f"   Запрос: '{visuals['query']}', клипов: {len(visuals['clips'])}")

print("\n5. Собираю видео...")
video = assemble_video(
    voice["audio_path"],
    platform,
    clips=visuals["clips"],
    subtitle_path=subs["subtitle_path"],
)
print(f"   Видео: {video['video_path']}")
print(f"   Разрешение: {video['resolution']}, длительность: {video['duration']} сек")
print(f"   Фон: {video['background']}")

print("\nГотово!")
