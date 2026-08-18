from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

try:
    import websocket
except ImportError:
    websocket = None


# ============================================================
# PATHS / CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
WORKFLOW_PATH = BASE_DIR / "workflow.json"
GENERATED_DIR = BASE_DIR / "generated_videos"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
COMFYUI_WS_URL = os.getenv(
    "COMFYUI_WS_URL",
    COMFYUI_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws",
)

COMFYUI_ROOT = Path(
    os.getenv(
        "COMFYUI_ROOT",
        r"C:\Users\ramir\Downloads\ComfyUI_windows_portable",
    )
)
COMFYUI_LAUNCHER = COMFYUI_ROOT / "run_nvidia_gpu.bat"
COMFYUI_START_TIMEOUT_SECONDS = int(os.getenv("COMFYUI_START_TIMEOUT_SECONDS", "90"))

# Storage retention. Change these with environment variables if required.
MAX_GENERATED_FILES = int(os.getenv("MAX_GENERATED_FILES", "50"))
MAX_GENERATED_GB = float(os.getenv("MAX_GENERATED_GB", "10"))
FFMPEG_TIMEOUT_SECONDS = int(os.getenv("FFMPEG_TIMEOUT_SECONDS", "300"))


# ============================================================
# REQUEST MODEL
# ============================================================

class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    resolution: Literal["512x288", "768x432", "1024x576"] = "768x432"
    duration: Literal[3, 5, 8] = 5
    quality: Literal["fast", "standard", "high"] = "standard"


# ============================================================
# GENERATION CONFIG
# ============================================================

RESOLUTION_MAP = {
    "512x288": (512, 288),
    "768x432": (768, 432),
    "1024x576": (1024, 576),
}

# 16 FPS relationship and 4n+1 latent-frame requirement.
DURATION_FRAME_MAP = {
    3: 49,
    5: 81,
    8: 129,
}

QUALITY_STEPS_MAP = {
    "fast": 15,
    "standard": 25,
    "high": 35,
}


# ============================================================
# IN-MEMORY SINGLE-GPU QUEUE
# ============================================================

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.RLock()
JOB_QUEUE: queue.Queue[str] = queue.Queue()
WORKER_THREAD: threading.Thread | None = None
STOP_EVENT = threading.Event()


def now() -> float:
    return time.time()


def update_job(job_id: str, **updates: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = now()


def get_job(job_id: str) -> dict[str, Any] | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def create_job(request: GenerateRequest) -> str:
    job_id = str(uuid.uuid4())

    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "prompt_id": None,
            "client_id": None,
            "status": "queued",
            "stage": "Queued",
            "progress": 1,
            "message": "Your request is waiting for the GPU.",
            "node": None,
            "prompt": request.prompt.strip(),
            "resolution": request.resolution,
            "duration": request.duration,
            "quality": request.quality,
            "frames": DURATION_FRAME_MAP[request.duration],
            "steps": QUALITY_STEPS_MAP[request.quality],
            "video_url": None,
            "thumbnail_url": None,
            "error": None,
            "created_at": now(),
            "updated_at": now(),
        }

    JOB_QUEUE.put(job_id)
    return job_id


def get_queue_position(job_id: str) -> int | None:
    with JOBS_LOCK:
        queued = [
            job for job in JOBS.values()
            if job["status"] == "queued"
        ]
        queued.sort(key=lambda job: job["created_at"])
        for index, job in enumerate(queued, start=1):
            if job["job_id"] == job_id:
                return index
    return None


def set_progress(
    job_id: str,
    progress: int | float,
    stage: str,
    message: str | None = None,
    node: str | None = None,
) -> None:
    updates: dict[str, Any] = {
        "progress": max(0, min(100, float(progress))),
        "stage": stage,
        "node": node,
    }

    if message is not None:
        updates["message"] = message

    update_job(job_id, **updates)


# ============================================================
# COMFYUI STARTUP
# ============================================================

def comfyui_is_running() -> bool:
    try:
        response = requests.get(f"{COMFYUI_URL}/system_stats", timeout=2)
        return response.ok
    except requests.RequestException:
        return False


def start_comfyui_if_needed() -> None:
    if comfyui_is_running():
        print("[COMFYUI] Already running.")
        return

    if not COMFYUI_LAUNCHER.is_file():
        raise RuntimeError(
            "ComfyUI is not running and its portable installation was not found at "
            f"{COMFYUI_ROOT}. Expected launcher: {COMFYUI_LAUNCHER}. "
            "Set COMFYUI_ROOT if your ComfyUI folder is elsewhere."
        )

    print(f"[COMFYUI] Starting from {COMFYUI_ROOT}...")
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    subprocess.Popen(
        ["cmd.exe", "/c", str(COMFYUI_LAUNCHER)],
        cwd=str(COMFYUI_ROOT),
        creationflags=creationflags,
    )

    deadline = time.monotonic() + COMFYUI_START_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        if comfyui_is_running():
            print("[COMFYUI] Ready.")
            return
        time.sleep(1)

    raise RuntimeError(
        f"ComfyUI was started but its API did not become available within "
        f"{COMFYUI_START_TIMEOUT_SECONDS} seconds."
    )


# ============================================================
# COMFYUI
# ============================================================

def load_workflow() -> dict[str, Any]:
    if not WORKFLOW_PATH.exists():
        raise FileNotFoundError(
            f"workflow.json was not found at: {WORKFLOW_PATH}"
        )

    with WORKFLOW_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def comfy_get(path: str, timeout: int = 30) -> requests.Response:
    try:
        response = requests.get(
            f"{COMFYUI_URL}{path}",
            timeout=timeout,
        )
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not contact ComfyUI: {exc}") from exc


def submit_to_comfy(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return

    workflow = load_workflow()

    # Node 6 = positive CLIP text encoder.
    if "6" not in workflow:
        raise RuntimeError(
            "Could not find positive prompt node (6) in workflow."
        )
    workflow["6"]["inputs"]["text"] = job["prompt"]

    # Node 40 = EmptyHunyuanLatentVideo.
    if "40" not in workflow:
        raise RuntimeError(
            "Could not find video latent node (40) in workflow."
        )

    width, height = RESOLUTION_MAP[job["resolution"]]
    workflow["40"]["inputs"]["width"] = width
    workflow["40"]["inputs"]["height"] = height
    workflow["40"]["inputs"]["length"] = job["frames"]

    # Node 3 = KSampler.
    if "3" not in workflow:
        raise RuntimeError("Could not find KSampler node (3) in workflow.")
    workflow["3"]["inputs"]["steps"] = job["steps"]

    client_id = str(uuid.uuid4())

    update_job(
        job_id,
        status="processing",
        stage="Queueing on ComfyUI",
        progress=3,
        message="Sending the workflow to ComfyUI...",
        client_id=client_id,
    )

    try:
        response = requests.post(
            f"{COMFYUI_URL}/prompt",
            json={
                "prompt": workflow,
                "client_id": client_id,
            },
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(
            f"Failed to submit workflow to ComfyUI: {exc}"
        ) from exc

    prompt_id = result.get("prompt_id")

    if not prompt_id:
        raise RuntimeError("ComfyUI did not return a prompt_id.")

    update_job(
        job_id,
        prompt_id=prompt_id,
        status="processing",
        stage="Queued",
        progress=4,
        message="ComfyUI accepted the generation.",
    )

    # WebSocket is best-effort. HTTP history remains authoritative.
    if websocket is not None:
        threading.Thread(
            target=track_comfy_progress,
            args=(job_id, client_id, prompt_id),
            daemon=True,
            name=f"progress-{job_id[:8]}",
        ).start()


def track_comfy_progress(
    job_id: str,
    client_id: str,
    prompt_id: str,
) -> None:
    """Read ComfyUI's real progress events.

    The UI reserves 5-90% for actual model execution. Conversion and
    thumbnail generation use the final 10%.
    """
    if websocket is None:
        return

    ws = None

    try:
        ws = websocket.create_connection(
            f"{COMFYUI_WS_URL}?clientId={client_id}",
            timeout=5,
        )

        while True:
            try:
                raw = ws.recv()
            except Exception:
                break

            if not raw or isinstance(raw, bytes):
                continue

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            message_type = message.get("type")
            data = message.get("data", {})

            event_prompt_id = data.get("prompt_id")

            if event_prompt_id and event_prompt_id != prompt_id:
                continue

            if message_type == "progress":
                value = float(data.get("value", 0))
                maximum = float(data.get("max", 1) or 1)
                ratio = max(0.0, min(1.0, value / maximum))

                set_progress(
                    job_id,
                    5 + ratio * 85,
                    "Generating frames",
                    f"Generating your video... {round(ratio * 100)}%",
                )

            elif message_type == "executing":
                node = data.get("node")
                update_job(job_id, node=str(node) if node else None)

    except Exception as exc:
        print(
            f"[WEBSOCKET] Progress tracker stopped for "
            f"{prompt_id}: {exc}"
        )

    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def wait_for_comfy_completion(
    job_id: str,
    prompt_id: str,
) -> dict[str, Any]:
    """Use /history as the authoritative completion signal."""
    while not STOP_EVENT.is_set():
        try:
            history = comfy_get(
                f"/history/{prompt_id}",
                timeout=10,
            ).json()
        except Exception as exc:
            update_job(
                job_id,
                status="processing",
                message=f"Waiting for ComfyUI... ({exc})",
            )
            time.sleep(2)
            continue

        if prompt_id not in history:
            update_job(
                job_id,
                status="processing",
                stage="Queued",
                message="Waiting for the GPU to start this job...",
            )
            time.sleep(1.5)
            continue

        comfy_job = history[prompt_id]
        status = comfy_job.get("status", {})
        status_str = status.get("status_str", "unknown")

        if status_str == "error":
            raise RuntimeError("ComfyUI reported a generation error.")

        if status.get("completed") is True:
            return comfy_job

        time.sleep(1.5)

    raise RuntimeError("Generation worker stopped.")


# ============================================================
# FFMPEG / MEDIA
# ============================================================

def get_ffmpeg_path() -> str:
    ffmpeg = shutil.which("ffmpeg")

    if not ffmpeg:
        raise RuntimeError(
            "FFmpeg was not found on PATH. Run 'ffmpeg -version' first."
        )

    return ffmpeg


def get_ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def download_comfy_file(
    filename: str,
    subfolder: str,
    file_type: str,
    destination: Path,
) -> Path:
    try:
        response = requests.get(
            f"{COMFYUI_URL}/view",
            params={
                "filename": filename,
                "subfolder": subfolder,
                "type": file_type,
            },
            timeout=120,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Could not download ComfyUI output: {exc}"
        ) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(response.content)

    return destination


def probe_duration_seconds(source: Path) -> float | None:
    ffprobe = get_ffprobe_path()

    if not ffprobe:
        return None

    command = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return None

        return float(result.stdout.strip())

    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None


def convert_webp_to_mp4(
    job_id: str,
    source: Path,
) -> Path:
    ffmpeg = get_ffmpeg_path()

    output = GENERATED_DIR / f"{job_id}.mp4"
    temp_output = GENERATED_DIR / f"{job_id}.tmp.mp4"

    if output.exists() and output.stat().st_size > 0:
        return output

    if not source.exists() or source.stat().st_size == 0:
        raise RuntimeError(
            "The generated WebP is missing or empty."
        )

    temp_output.unlink(missing_ok=True)

    duration = probe_duration_seconds(source)

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(source),
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-threads", "0",
        "-progress", "pipe:1",
        str(temp_output),
    ]

    set_progress(
        job_id,
        92,
        "Converting to MP4",
        "Frames are ready. Converting to MP4...",
    )

    process = None

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        while True:
            line = (
                process.stdout.readline()
                if process.stdout is not None
                else ""
            )

            if not line:
                if process.poll() is not None:
                    break
                time.sleep(0.1)
                continue

            line = line.strip()

            if line.startswith("out_time_ms=") and duration:
                try:
                    out_time_us = int(line.split("=", 1)[1])
                    seconds = out_time_us / 1_000_000
                    ratio = max(
                        0.0,
                        min(1.0, seconds / duration),
                    )

                    set_progress(
                        job_id,
                        92 + ratio * 7,
                        "Converting to MP4",
                        f"Converting to MP4... {round(ratio * 100)}%",
                    )
                except ValueError:
                    pass

        stdout, stderr = process.communicate(
            timeout=FFMPEG_TIMEOUT_SECONDS
        )

    except subprocess.TimeoutExpired:
        if process is not None:
            process.kill()
            process.communicate()

        temp_output.unlink(missing_ok=True)
        raise RuntimeError("FFmpeg conversion timed out.")

    except OSError as exc:
        temp_output.unlink(missing_ok=True)
        raise RuntimeError(
            f"Could not start FFmpeg: {exc}"
        ) from exc

    if (
        process.returncode != 0
        or not temp_output.exists()
    ):
        error = (
            stderr
            or stdout
            or "FFmpeg returned an unknown error"
        ).strip()

        if len(error) > 3000:
            error = error[-3000:]

        temp_output.unlink(missing_ok=True)

        raise RuntimeError(
            f"FFmpeg conversion failed: {error}"
        )

    temp_output.replace(output)

    return output


def make_thumbnail(
    job_id: str,
    source_mp4: Path,
) -> Path | None:
    ffmpeg = get_ffmpeg_path()

    thumbnail = GENERATED_DIR / f"{job_id}.jpg"
    temp = GENERATED_DIR / f"{job_id}.tmp.jpg"

    if thumbnail.exists() and thumbnail.stat().st_size > 0:
        return thumbnail

    temp.unlink(missing_ok=True)

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", "0.5",
        "-i", str(source_mp4),
        "-frames:v", "1",
        "-q:v", "3",
        str(temp),
    ]

    set_progress(
        job_id,
        99,
        "Creating thumbnail",
        "Creating a preview image...",
    )

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        temp.unlink(missing_ok=True)
        print(f"[THUMBNAIL] Failed: {exc}")
        return None

    if result.returncode != 0 or not temp.exists():
        temp.unlink(missing_ok=True)
        print(
            "[THUMBNAIL] FFmpeg failed: "
            f"{result.stderr}"
        )
        return None

    temp.replace(thumbnail)

    return thumbnail


def build_result_files(
    job_id: str,
    comfy_job: dict[str, Any],
) -> dict[str, str]:
    outputs = comfy_job.get("outputs", {})

    # Current workflow: node 28 = SaveAnimatedWEBP.
    for node_id, node_output in outputs.items():
        for image in node_output.get("images", []):
            filename = image.get("filename")

            if (
                not filename
                or not filename.lower().endswith(".webp")
            ):
                continue

            source = GENERATED_DIR / f"{job_id}.webp"

            download_comfy_file(
                filename,
                image.get("subfolder", ""),
                image.get("type", "output"),
                source,
            )

            mp4 = convert_webp_to_mp4(
                job_id,
                source,
            )

            # WebP is only an intermediate artifact.
            source.unlink(missing_ok=True)

            thumbnail = make_thumbnail(
                job_id,
                mp4,
            )

            return {
                "filename": mp4.name,
                "thumbnail": (
                    thumbnail.name
                    if thumbnail
                    else ""
                ),
                "node_id": str(node_id),
            }

    # Fallback for native video outputs.
    for node_id, node_output in outputs.items():
        for video in node_output.get("videos", []):
            filename = video.get("filename")

            if not filename:
                continue

            destination = (
                GENERATED_DIR /
                f"{job_id}.mp4"
            )

            download_comfy_file(
                filename,
                video.get("subfolder", ""),
                video.get("type", "output"),
                destination,
            )

            thumbnail = make_thumbnail(
                job_id,
                destination,
            )

            return {
                "filename": destination.name,
                "thumbnail": (
                    thumbnail.name
                    if thumbnail
                    else ""
                ),
                "node_id": str(node_id),
            }

    raise RuntimeError(
        "Generation completed, but no video output was found."
    )


# ============================================================
# STORAGE RETENTION
# ============================================================

def cleanup_storage(
    exclude_job_id: str | None = None,
) -> None:
    try:
        for path in GENERATED_DIR.glob("*.tmp.*"):
            path.unlink(missing_ok=True)

        videos: list[Path] = []

        for path in GENERATED_DIR.glob("*.mp4"):
            if exclude_job_id and path.stem == exclude_job_id:
                continue
            videos.append(path)

        videos.sort(
            key=lambda path: path.stat().st_mtime
        )

        def existing_videos() -> list[Path]:
            return [
                path
                for path in videos
                if path.exists()
            ]

        def total_bytes() -> int:
            return sum(
                path.stat().st_size
                for path in existing_videos()
            )

        max_bytes = (
            MAX_GENERATED_GB *
            1024 *
            1024 *
            1024
        )

        while (
            len(existing_videos()) >
            MAX_GENERATED_FILES
            or total_bytes() > max_bytes
        ):
            current = existing_videos()

            if not current:
                break

            oldest = current[0]
            oldest.unlink(missing_ok=True)

            (
                GENERATED_DIR /
                f"{oldest.stem}.jpg"
            ).unlink(missing_ok=True)

    except OSError as exc:
        print(f"[STORAGE] Cleanup failed: {exc}")


# ============================================================
# GENERATION WORKER
# ============================================================

def process_job(job_id: str) -> None:
    try:
        update_job(
            job_id,
            status="processing",
            stage="Preparing",
            progress=2,
            message="Preparing the generation...",
        )

        submit_to_comfy(job_id)

        job = get_job(job_id)

        if not job or not job.get("prompt_id"):
            raise RuntimeError(
                "Generation was submitted without a prompt ID."
            )

        prompt_id = job["prompt_id"]

        comfy_job = wait_for_comfy_completion(
            job_id,
            prompt_id,
        )

        set_progress(
            job_id,
            90,
            "Frames ready",
            "Frames are ready. Preparing the final video...",
        )

        result = build_result_files(
            job_id,
            comfy_job,
        )

        video_url = (
            f"/generated-video/{result['filename']}"
        )

        thumbnail_url = (
            f"/generated-thumbnail/"
            f"{result['thumbnail']}"
            if result["thumbnail"]
            else None
        )

        update_job(
            job_id,
            status="ready",
            stage="Ready",
            progress=100,
            message="Video generation completed.",
            video_url=video_url,
            thumbnail_url=thumbnail_url,
            node=result["node_id"],
            error=None,
        )

        cleanup_storage(
            exclude_job_id=job_id
        )

    except Exception as exc:
        print(
            f"[JOB {job_id}] ERROR: {exc}"
        )

        update_job(
            job_id,
            status="error",
            stage="Error",
            progress=0,
            message="Video generation failed.",
            error=str(exc),
        )


def generation_worker() -> None:
    print("[QUEUE] Single-GPU generation worker started.")

    while not STOP_EVENT.is_set():
        try:
            job_id = JOB_QUEUE.get(
                timeout=0.5
            )
        except queue.Empty:
            continue

        try:
            process_job(job_id)
        finally:
            JOB_QUEUE.task_done()

    print("[QUEUE] Generation worker stopped.")


def start_worker() -> None:
    global WORKER_THREAD

    if (
        WORKER_THREAD
        and WORKER_THREAD.is_alive()
    ):
        return

    STOP_EVENT.clear()

    WORKER_THREAD = threading.Thread(
        target=generation_worker,
        daemon=True,
        name="generation-worker",
    )

    WORKER_THREAD.start()


# ============================================================
# SYSTEM STATUS
# ============================================================

def get_comfyui_status() -> dict[str, Any]:
    try:
        response = requests.get(
            f"{COMFYUI_URL}/system_stats",
            timeout=2,
        )

        if response.ok:
            return {
                "status": "connected",
                "url": COMFYUI_URL,
                "message": "ComfyUI is connected.",
            }

        return {
            "status": "error",
            "url": COMFYUI_URL,
            "message": (
                f"ComfyUI returned HTTP "
                f"{response.status_code}."
            ),
        }

    except requests.RequestException:
        return {
            "status": "disconnected",
            "url": COMFYUI_URL,
            "message": "ComfyUI is not reachable.",
        }


def get_ffmpeg_status() -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if ffmpeg and ffprobe:
        status = "available"
        message = (
            "FFmpeg and FFprobe are available."
        )
    elif ffmpeg:
        status = "available"
        message = (
            "FFmpeg is available. "
            "Conversion progress may be approximate."
        )
    else:
        status = "missing"
        message = (
            "FFmpeg was not found on PATH."
        )

    return {
        "status": status,
        "path": ffmpeg,
        "ffprobe": bool(ffprobe),
        "message": message,
    }


def get_storage_status() -> dict[str, Any]:
    try:
        files = [
            item
            for item in GENERATED_DIR.iterdir()
            if item.is_file()
        ]

        used_bytes = sum(
            item.stat().st_size
            for item in files
        )

        disk_usage = shutil.disk_usage(
            GENERATED_DIR
        )

        return {
            "status": "available",
            "generated_dir": str(GENERATED_DIR),
            "generated_files": len(files),
            "generated_bytes": used_bytes,
            "free_bytes": disk_usage.free,
            "retention_max_files": MAX_GENERATED_FILES,
            "retention_max_gb": MAX_GENERATED_GB,
            "message": (
                "Generated video storage "
                "is available."
            ),
        }

    except OSError as exc:
        return {
            "status": "error",
            "generated_dir": str(GENERATED_DIR),
            "generated_files": 0,
            "generated_bytes": 0,
            "free_bytes": None,
            "message": (
                f"Storage check failed: {exc}"
            ),
        }


def get_generation_summary() -> dict[str, Any]:
    with JOBS_LOCK:
        active = [
            job
            for job in JOBS.values()
            if job["status"]
            in {"queued", "processing"}
        ]

        latest_active = max(
            active,
            key=lambda job: job["updated_at"],
            default=None,
        )

        latest = latest_active or max(
            JOBS.values(),
            key=lambda job: job["updated_at"],
            default=None,
        )

    if any(
        job["status"] == "processing"
        for job in active
    ):
        status = "generating"
    elif active:
        status = "queued"
    else:
        status = "idle"

    return {
        "status": status,
        "stage": (
            latest["stage"]
            if latest
            else "Ready"
        ),
        "progress": (
            latest["progress"]
            if latest
            else 0
        ),
        "message": (
            latest["message"]
            if latest
            else "No active generation."
        ),
        "job_id": (
            latest["job_id"]
            if latest
            else None
        ),
        "prompt_id": (
            latest.get("prompt_id")
            if latest
            else None
        ),
        "queue_size": JOB_QUEUE.qsize(),
    }


def build_system_status() -> dict[str, Any]:
    comfyui = get_comfyui_status()
    ffmpeg = get_ffmpeg_status()
    storage = get_storage_status()
    generation = get_generation_summary()

    overall = "ok"

    if comfyui["status"] != "connected":
        overall = "degraded"

    if ffmpeg["status"] != "available":
        overall = "degraded"

    if storage["status"] != "available":
        overall = "degraded"

    return {
        "status": overall,
        "backend": {
            "status": "ok",
            "message": (
                "FastAPI backend is running."
            ),
        },
        "comfyui": comfyui,
        "ffmpeg": ffmpeg,
        "storage": storage,
        "generation": generation,
    }


# ============================================================
# FASTAPI
# ============================================================

@asynccontextmanager
async def lifespan(_: FastAPI):
    start_comfyui_if_needed()
    start_worker()
    yield
    STOP_EVENT.set()


app = FastAPI(
    title="MotionForge AI Video Generator",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def root():
    return {
        "message": "MotionForge backend is running",
        "queue_size": JOB_QUEUE.qsize(),
    }


@app.get("/health")
def health():
    return build_system_status()


@app.get("/system/status")
def system_status():
    return build_system_status()


@app.get("/workflow")
def get_workflow():
    try:
        workflow = load_workflow()
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )

    return {
        "status": "success",
        "workflow": workflow,
    }


@app.post("/generate")
def generate_video(
    request: GenerateRequest,
):
    request.prompt = request.prompt.strip()

    if not request.prompt:
        raise HTTPException(
            status_code=400,
            detail="Prompt cannot be empty.",
        )

    if not comfyui_is_running():
        try:
            start_comfyui_if_needed()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503,
                detail=str(exc),
            )

    try:
        load_workflow()
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )

    job_id = create_job(request)

    # queue.Queue includes the currently running item only after the
    # worker has removed it, so this is an approximate queue position.
    queue_position = max(
        1,
        JOB_QUEUE.qsize(),
    )

    return {
        "status": "queued",
        "message": (
            "Generation queued."
            if queue_position > 1
            else "Generation is starting."
        ),
        "job_id": job_id,
        "queue_position": queue_position,
        "resolution": request.resolution,
        "duration": request.duration,
        "quality": request.quality,
        "frames": DURATION_FRAME_MAP[
            request.duration
        ],
        "steps": QUALITY_STEPS_MAP[
            request.quality
        ],
    }


@app.get("/status/{job_id}")
def get_status(job_id: str):
    job = get_job(job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Generation job not found.",
        )

    return {
        "status": job["status"],
        "job_id": job["job_id"],
        "prompt_id": job.get("prompt_id"),
        "progress": job["progress"],
        "stage": job["stage"],
        "node": job.get("node"),
        "message": job["message"],
        "queue_position": get_queue_position(job_id),
        "video_url": job.get("video_url"),
        "thumbnail_url": job.get(
            "thumbnail_url"
        ),
        "error": job.get("error"),
        "resolution": job["resolution"],
        "duration": job["duration"],
        "quality": job["quality"],
        "frames": job["frames"],
        "steps": job["steps"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


@app.get("/result/{job_id}")
def get_result(job_id: str):
    job = get_job(job_id)

    if not job:
        raise HTTPException(
            status_code=404,
            detail="Generation job not found.",
        )

    if job["status"] == "error":
        return {
            "status": "error",
            "job_id": job_id,
            "message": job["message"],
            "error": job.get("error"),
        }

    if (
        job["status"] != "ready"
        or not job.get("video_url")
    ):
        return {
            "status": job["status"],
            "job_id": job_id,
            "progress": job["progress"],
            "stage": job["stage"],
            "message": job["message"],
        }

    return {
        "status": "completed",
        "job_id": job_id,
        "message": (
            "Video generation completed."
        ),
        "files": [
            {
                "filename": Path(
                    job["video_url"]
                ).name,
                "type": "video",
                "format": "mp4",
                "url": job["video_url"],
                "thumbnail_url": job.get(
                    "thumbnail_url"
                ),
            }
        ],
    }


# ============================================================
# MEDIA SERVING
# ============================================================

def safe_generated_path(
    filename: str,
    suffix: str,
) -> Path:
    safe_filename = Path(filename).name

    if safe_filename != filename:
        raise HTTPException(
            status_code=400,
            detail="Invalid filename.",
        )

    if not safe_filename.lower().endswith(
        suffix
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid media type.",
        )

    path = GENERATED_DIR / safe_filename

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Generated media was not found.",
        )

    return path


@app.get("/generated-video/{filename}")
def get_generated_video(
    filename: str,
):
    path = safe_generated_path(
        filename,
        ".mp4",
    )

    return FileResponse(
        path=path,
        media_type="video/mp4",
        filename=path.name,
    )


@app.get("/generated-thumbnail/{filename}")
def get_generated_thumbnail(
    filename: str,
):
    path = safe_generated_path(
        filename,
        ".jpg",
    )

    return FileResponse(
        path=path,
        media_type="image/jpeg",
        filename=path.name,
    )


@app.get("/media")
def get_media(
    filename: str,
    subfolder: str = "",
    type: str = "output",
):
    try:
        response = requests.get(
            f"{COMFYUI_URL}/view",
            params={
                "filename": filename,
                "subfolder": subfolder,
                "type": type,
            },
            timeout=30,
        )
        response.raise_for_status()

    except requests.RequestException as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Could not contact ComfyUI: {exc}"
            ),
        )

    return Response(
        content=response.content,
        media_type=response.headers.get(
            "content-type",
            "application/octet-stream",
        ),
    )
