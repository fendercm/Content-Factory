import json
import pytest
from unittest.mock import MagicMock, patch

from agents.assembly_agent import assemble_video

FFPROBE_OUTPUT = json.dumps({
    "streams": [{"codec_type": "audio", "duration": "25.5"}]
})


def _subprocess_mock():
    m = MagicMock()
    m.stdout = FFPROBE_OUTPUT
    return m


# ── Happy path ───────────────────────────────────────────────────────────────

@patch("agents.assembly_agent.os.makedirs")
@patch("agents.assembly_agent.os.path.exists", return_value=True)
@patch("agents.assembly_agent.subprocess.run")
@patch("agents.assembly_agent.shutil.which", return_value="/usr/bin/ffmpeg")
def test_assemble_video_returns_required_keys(mock_which, mock_run, mock_exists, mock_makedirs):
    mock_run.return_value = _subprocess_mock()
    result = assemble_video("/fake/audio.mp3", "tiktok")
    assert {"video_path", "platform", "resolution", "duration", "background"} <= result.keys()


@pytest.mark.parametrize("platform,resolution", [
    ("tiktok",    "1080x1920"),
    ("instagram", "1080x1920"),
    ("youtube",   "1080x1920"),  # Shorts — вертикальный формат
])
@patch("agents.assembly_agent.os.makedirs")
@patch("agents.assembly_agent.os.path.exists", return_value=True)
@patch("agents.assembly_agent.subprocess.run")
@patch("agents.assembly_agent.shutil.which", return_value="/usr/bin/ffmpeg")
def test_assemble_video_platform_resolution(mock_which, mock_run, mock_exists, mock_makedirs, platform, resolution):
    mock_run.return_value = _subprocess_mock()
    result = assemble_video("/fake/audio.mp3", platform)
    assert result["resolution"] == resolution


@patch("agents.assembly_agent.os.makedirs")
@patch("agents.assembly_agent.os.path.exists", return_value=True)
@patch("agents.assembly_agent.subprocess.run")
@patch("agents.assembly_agent.shutil.which", return_value="/usr/bin/ffmpeg")
def test_assemble_video_black_background_when_no_clips(mock_which, mock_run, mock_exists, mock_makedirs):
    mock_run.return_value = _subprocess_mock()
    result = assemble_video("/fake/audio.mp3", "tiktok")
    assert result["background"] == "black"
    assert result["duration"] == 25.5
    assert result["video_path"].endswith(".mp4")

    ffmpeg_cmd = mock_run.call_args_list[-1][0][0]
    assert "-t" in ffmpeg_cmd
    assert str(25.5) in ffmpeg_cmd


@patch("agents.assembly_agent.os.makedirs")
@patch("agents.assembly_agent.os.path.exists", return_value=True)
@patch("agents.assembly_agent.subprocess.run")
@patch("agents.assembly_agent.shutil.which", return_value="/usr/bin/ffmpeg")
def test_assemble_video_with_clips_uses_filter_complex(mock_which, mock_run, mock_exists, mock_makedirs):
    mock_run.return_value = _subprocess_mock()
    result = assemble_video("/fake/audio.mp3", "tiktok", clips=["/fake/c1.mp4", "/fake/c2.mp4"])
    assert result["background"] == "clips"
    assert result["duration"] == 25.5

    ffmpeg_cmd = mock_run.call_args_list[-1][0][0]
    assert "-filter_complex" in ffmpeg_cmd
    assert "-stream_loop" in ffmpeg_cmd
    assert "-t" in ffmpeg_cmd


@patch("agents.assembly_agent.os.makedirs")
@patch("agents.assembly_agent.os.path.exists", return_value=True)
@patch("agents.assembly_agent.subprocess.run")
@patch("agents.assembly_agent.shutil.which", return_value="/usr/bin/ffmpeg")
def test_assemble_video_subtitle_in_filter_complex(mock_which, mock_run, mock_exists, mock_makedirs):
    mock_run.return_value = _subprocess_mock()
    assemble_video(
        "/fake/audio.mp3", "tiktok",
        clips=["/fake/c1.mp4"],
        subtitle_path="/fake/subs.ass",
    )
    ffmpeg_cmd = mock_run.call_args_list[-1][0][0]
    fc_idx = ffmpeg_cmd.index("-filter_complex")
    assert "subtitles=" in ffmpeg_cmd[fc_idx + 1]


@patch("agents.assembly_agent.os.makedirs")
@patch("agents.assembly_agent.os.path.exists", return_value=True)
@patch("agents.assembly_agent.subprocess.run")
@patch("agents.assembly_agent.shutil.which", return_value="/usr/bin/ffmpeg")
def test_assemble_video_black_subtitle_in_filter_complex(mock_which, mock_run, mock_exists, mock_makedirs):
    mock_run.return_value = _subprocess_mock()
    assemble_video("/fake/audio.mp3", "tiktok", subtitle_path="/fake/subs.ass")
    ffmpeg_cmd = mock_run.call_args_list[-1][0][0]
    fc_idx = ffmpeg_cmd.index("-filter_complex")
    assert "subtitles=" in ffmpeg_cmd[fc_idx + 1]


# ── Validation errors ────────────────────────────────────────────────────────

@patch("agents.assembly_agent.shutil.which", return_value="/usr/bin/ffmpeg")
def test_assemble_video_invalid_platform_raises(mock_which):
    with pytest.raises(ValueError, match="Неподдерживаемая платформа"):
        assemble_video("/fake/audio.mp3", "facebook")


@patch("agents.assembly_agent.shutil.which", return_value=None)
def test_assemble_video_ffmpeg_not_installed_raises(mock_which):
    with pytest.raises(EnvironmentError, match="ffmpeg/ffprobe не найден"):
        assemble_video("/fake/audio.mp3", "tiktok")


@patch("agents.assembly_agent.os.path.exists", return_value=False)
@patch("agents.assembly_agent.shutil.which", return_value="/usr/bin/ffmpeg")
def test_assemble_video_missing_audio_raises(mock_which, mock_exists):
    with pytest.raises(ValueError, match="Аудио файл не найден"):
        assemble_video("/nonexistent/audio.mp3", "tiktok")


@patch("agents.assembly_agent.shutil.which", return_value="/usr/bin/ffmpeg")
def test_assemble_video_missing_clips_raises(mock_which):
    def exists_side_effect(path):
        return str(path).endswith(".mp3")

    with patch("agents.assembly_agent.os.path.exists", side_effect=exists_side_effect):
        with pytest.raises(ValueError, match="Клипы не найдены"):
            assemble_video("/fake/audio.mp3", "tiktok", clips=["/fake/clip.mp4"])
