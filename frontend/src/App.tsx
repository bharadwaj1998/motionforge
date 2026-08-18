import { useEffect, useRef, useState } from "react";
import "./App.css";

const API_BASE =
  import.meta.env.VITE_API_BASE ??
  "http://127.0.0.1:8000";

const ACTIVE_JOB_KEY =
  "motionforge-active-job-v3";

const HISTORY_STORAGE_KEY =
  "motionforge-history-v3";

type Resolution =
  | "512x288"
  | "768x432"
  | "1024x576";

type Duration = 3 | 5 | 8;

type Quality =
  | "fast"
  | "standard"
  | "high";

type GenerateRequest = {
  prompt: string;
  resolution: Resolution;
  duration: Duration;
  quality: Quality;
};

type GenerateResponse = {
  job_id?: string;
  status?: string;
  message?: string;
  queue_position?: number;
};

type StatusResponse = {
  status?: string;
  job_id?: string;
  prompt_id?: string | null;
  progress?: number;
  stage?: string;
  node?: string | null;
  message?: string;
  queue_position?: number | null;
  video_url?: string | null;
  thumbnail_url?: string | null;
  error?: string | null;
  resolution?: Resolution;
  duration?: Duration;
  quality?: Quality;
};

type SystemStatus = {
  status?: string;
  backend?: {
    status?: string;
    message?: string;
  };
  comfyui?: {
    status?: string;
    message?: string;
  };
  ffmpeg?: {
    status?: string;
    message?: string;
    ffprobe?: boolean;
  };
  storage?: {
    status?: string;
    generated_files?: number;
    generated_bytes?: number;
    free_bytes?: number | null;
    retention_max_files?: number;
    retention_max_gb?: number;
  };
  generation?: {
    status?: string;
    stage?: string;
    progress?: number;
    message?: string;
    queue_size?: number;
  };
};

type HistoryItem = {
  id: string;
  prompt: string;
  resolution: Resolution;
  duration: Duration;
  quality: Quality;
  videoUrl: string;
  thumbnailUrl?: string | null;
  createdAt: string;
};

const PROMPT_PRESETS = [
  {
    name: "Cinematic Street",
    prompt:
      "A person walking through a quiet city street just after sunset, wet pavement reflecting warm shop lights, cars slowly passing in the background, natural movement, realistic camera motion, calm cinematic atmosphere.",
  },
  {
    name: "Nature",
    prompt:
      "A peaceful mountain valley at sunrise, soft golden light moving across the landscape, mist drifting between the trees, a gentle breeze moving the grass, slow cinematic camera movement, realistic natural details.",
  },
  {
    name: "Sci-Fi",
    prompt:
      "A futuristic city at night filled with glowing signs and flying vehicles, rain falling through neon lights, people walking through the streets, reflections on wet pavement, cinematic camera movement, realistic atmosphere.",
  },
  {
    name: "Product Shot",
    prompt:
      "A premium smartphone standing on a dark reflective table, soft studio lighting moving across its surface, subtle camera rotation, elegant shadows, realistic materials, high-end commercial product photography.",
  },
];

function absoluteUrl(url?: string | null) {
  if (!url) return null;
  if (url.startsWith("http://") || url.startsWith("https://")) {
    return url;
  }
  return `${API_BASE}${url}`;
}

function App() {
  const [prompt, setPrompt] = useState(
    PROMPT_PRESETS[0].prompt,
  );

  const [resolution, setResolution] =
    useState<Resolution>("768x432");

  const [duration, setDuration] =
    useState<Duration>(5);

  const [quality, setQuality] =
    useState<Quality>("standard");

  const [isGenerating, setIsGenerating] =
    useState(false);

  const [statusMessage, setStatusMessage] =
    useState("");

  const [stage, setStage] =
    useState("Ready");

  const [progress, setProgress] =
    useState(0);

  const [node, setNode] =
    useState<string | null>(null);

  const [queuePosition, setQueuePosition] =
    useState<number | null>(null);

  const [, setJobId] =
    useState<string | null>(null);

  const [videoUrl, setVideoUrl] =
    useState<string | null>(null);

  const [thumbnailUrl, setThumbnailUrl] =
    useState<string | null>(null);

  const [error, setError] =
    useState("");

  const [systemStatus, setSystemStatus] =
    useState<SystemStatus | null>(null);

  const [systemError, setSystemError] =
    useState("");

  const [history, setHistory] =
    useState<HistoryItem[]>(() => {
      try {
        const saved =
          localStorage.getItem(
            HISTORY_STORAGE_KEY,
          );

        if (!saved) return [];

        const parsed =
          JSON.parse(saved);

        return Array.isArray(parsed)
          ? parsed
          : [];
      } catch {
        return [];
      }
    });

  const pollTimer =
    useRef<number | null>(null);

  const systemTimer =
    useRef<number | null>(null);

  useEffect(() => {
    try {
      localStorage.setItem(
        HISTORY_STORAGE_KEY,
        JSON.stringify(history),
      );
    } catch (storageError) {
      console.error(
        "Could not save history:",
        storageError,
      );
    }
  }, [history]);

  useEffect(() => {
    return () => {
      if (pollTimer.current !== null) {
        window.clearTimeout(
          pollTimer.current,
        );
      }

      if (systemTimer.current !== null) {
        window.clearInterval(
          systemTimer.current,
        );
      }
    };
  }, []);

  useEffect(() => {
    let stopped = false;

    const fetchSystemStatus = async () => {
      try {
        const response = await fetch(
          `${API_BASE}/system/status`,
        );

        if (!response.ok) {
          throw new Error(
            `System status failed (${response.status})`,
          );
        }

        const data: SystemStatus =
          await response.json();

        if (!stopped) {
          setSystemStatus(data);
          setSystemError("");
        }
      } catch (err) {
        if (!stopped) {
          setSystemError(
            err instanceof Error
              ? err.message
              : "Unable to check system status.",
          );
        }
      }
    };

    fetchSystemStatus();

    systemTimer.current =
      window.setInterval(
        fetchSystemStatus,
        5000,
      );

    return () => {
      stopped = true;

      if (systemTimer.current !== null) {
        window.clearInterval(
          systemTimer.current,
        );
      }
    };
  }, []);

  useEffect(() => {
    const savedJob =
      localStorage.getItem(
        ACTIVE_JOB_KEY,
      );

    if (!savedJob) return;

    try {
      const parsed = JSON.parse(savedJob);

      if (parsed?.jobId) {
        setJobId(parsed.jobId);
        setIsGenerating(true);
        setStage("Recovering");
        setStatusMessage(
          "Reconnecting to your generation...",
        );

        pollStatus(parsed.jobId);
      }
    } catch {
      localStorage.removeItem(
        ACTIVE_JOB_KEY,
      );
    }
    // Intentionally runs once on startup.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const formatResolution = (
    value: Resolution,
  ) => {
    const [width, height] =
      value.split("x");

    return `${width} × ${height}`;
  };

  const formatQuality = (
    value: Quality,
  ) => {
    if (value === "fast") return "Fast";
    if (value === "high") return "High";
    return "Standard";
  };

  const formatBytes = (
    value?: number | null,
  ) => {
    if (
      value === undefined ||
      value === null ||
      Number.isNaN(value)
    ) {
      return "Unknown";
    }

    const units = [
      "B",
      "KB",
      "MB",
      "GB",
      "TB",
    ];

    let size = value;
    let unit = 0;

    while (
      size >= 1024 &&
      unit < units.length - 1
    ) {
      size /= 1024;
      unit += 1;
    }

    return `${size.toFixed(
      unit === 0 ? 0 : 1,
    )} ${units[unit]}`;
  };

  const formatHistoryDate = (
    value: string,
  ) => {
    try {
      return new Date(
        value,
      ).toLocaleString(
        undefined,
        {
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
        },
      );
    } catch {
      return "";
    }
  };

  const addToHistory = (
    generatedUrl: string,
    generatedThumbnail?: string | null,
  ) => {
    const item: HistoryItem = {
      id:
        typeof crypto !== "undefined" &&
        crypto.randomUUID
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random()}`,
      prompt: prompt.trim(),
      resolution,
      duration,
      quality,
      videoUrl: generatedUrl,
      thumbnailUrl:
        generatedThumbnail,
      createdAt:
        new Date().toISOString(),
    };

    setHistory((current) => [
      item,
      ...current,
    ]);
  };

  const selectPreset = (
    presetPrompt: string,
  ) => {
    if (isGenerating) return;

    setPrompt(presetPrompt);
    setError("");
    setVideoUrl(null);
    setThumbnailUrl(null);
    setStatusMessage("");
    setProgress(0);
    setStage("Ready");
  };

  const clearVideo = () => {
    if (isGenerating) return;

    setVideoUrl(null);
    setThumbnailUrl(null);
    setError("");
    setProgress(0);
    setStatusMessage("");
    setStage("Ready");
    setNode(null);
  };

  const generateAnother = () => {
    clearVideo();

    window.scrollTo({
      top: 0,
      behavior: "smooth",
    });
  };

  const playHistoryItem = (
    item: HistoryItem,
  ) => {
    setPrompt(item.prompt);
    setResolution(item.resolution);
    setDuration(item.duration);
    setQuality(item.quality);

    setVideoUrl(
      absoluteUrl(item.videoUrl),
    );

    setThumbnailUrl(
      absoluteUrl(item.thumbnailUrl),
    );

    setError("");
    setStage("Ready");
    setProgress(100);
    setStatusMessage(
      "Previous generation loaded.",
    );

    window.scrollTo({
      top: 0,
      behavior: "smooth",
    });
  };

  const deleteHistoryItem = (
    id: string,
  ) => {
    setHistory((current) =>
      current.filter(
        (item) => item.id !== id,
      ),
    );
  };

  const clearHistory = () => {
    if (
      window.confirm(
        "Clear all generation history?",
      )
    ) {
      setHistory([]);
    }
  };

  const pollStatus = async (
    currentJobId: string,
  ) => {
    try {
      const response = await fetch(
        `${API_BASE}/status/${currentJobId}`,
        {
          cache: "no-store",
        },
      );

      if (!response.ok) {
        if (response.status === 404) {
          localStorage.removeItem(
            ACTIVE_JOB_KEY,
          );
          throw new Error(
            "This generation is no longer available. The backend may have restarted.",
          );
        }

        throw new Error(
          `Status request failed (${response.status})`,
        );
      }

      const data: StatusResponse =
        await response.json();

      const currentProgress =
        typeof data.progress === "number"
          ? data.progress
          : 0;

      setProgress(
        Math.min(
          100,
          Math.max(
            0,
            currentProgress,
          ),
        ),
      );

      setStage(
        data.stage || "Generating",
      );

      setNode(
        data.node || null,
      );

      setStatusMessage(
        data.message || "",
      );

      setQueuePosition(
        data.queue_position ??
          null,
      );

      if (
        data.resolution
      ) {
        setResolution(
          data.resolution,
        );
      }

      if (
        data.duration
      ) {
        setDuration(
          data.duration,
        );
      }

      if (
        data.quality
      ) {
        setQuality(
          data.quality,
        );
      }

      if (
        data.status === "error"
      ) {
        throw new Error(
          data.error ||
            data.message ||
            "Video generation failed.",
        );
      }

      if (
        data.status === "ready"
      ) {
        const url =
          absoluteUrl(
            data.video_url,
          );

        const thumbnail =
          absoluteUrl(
            data.thumbnail_url,
          );

        if (!url) {
          throw new Error(
            "Generation completed but no video URL was returned.",
          );
        }

        setVideoUrl(url);
        setThumbnailUrl(
          thumbnail,
        );
        setProgress(100);
        setStage("Ready");
        setStatusMessage(
          "Video generated successfully!",
        );
        setIsGenerating(false);

        localStorage.removeItem(
          ACTIVE_JOB_KEY,
        );

        addToHistory(
          url,
          thumbnail,
        );

        return;
      }

      pollTimer.current =
        window.setTimeout(
          () =>
            pollStatus(
              currentJobId,
            ),
          1200,
        );
    } catch (err) {
      console.error(
        "STATUS ERROR:",
        err,
      );

      setIsGenerating(false);
      setStage("Error");
      setProgress(0);

      setError(
        err instanceof Error
          ? err.message
          : "Unable to check generation status.",
      );

      setStatusMessage("");
      localStorage.removeItem(
        ACTIVE_JOB_KEY,
      );
    }
  };

  const generateVideo = async () => {
    if (!prompt.trim()) {
      setError(
        "Please describe the video you want to create.",
      );
      return;
    }

    if (pollTimer.current !== null) {
      window.clearTimeout(
        pollTimer.current,
      );
    }

    setIsGenerating(true);
    setError("");
    setVideoUrl(null);
    setThumbnailUrl(null);
    setProgress(1);
    setStage("Queued");
    setNode(null);
    setQueuePosition(null);
    setStatusMessage(
      "Sending your idea to the generation queue...",
    );

    try {
      const request: GenerateRequest = {
        prompt: prompt.trim(),
        resolution,
        duration,
        quality,
      };

      const response = await fetch(
        `${API_BASE}/generate`,
        {
          cache: "no-store",
          method: "POST",
          headers: {
            "Content-Type":
              "application/json",
          },
          body: JSON.stringify(
            request,
          ),
        },
      );

      if (!response.ok) {
        let message =
          `Generation request failed (${response.status})`;

        try {
          const errorData =
            await response.json();

          if (
            errorData?.detail
          ) {
            message =
              errorData.detail;
          }
        } catch {
          // Ignore invalid error JSON.
        }

        throw new Error(message);
      }

      const rawResponse = await response.text();

      let data: GenerateResponse;

      try {
        data = JSON.parse(rawResponse) as GenerateResponse;
      } catch {
        throw new Error(
          `Backend returned invalid JSON: ${rawResponse.slice(0, 500)}`,
        );
      }

      console.log("MotionForge /generate response:", data);

      if (!data.job_id) {
        throw new Error(
          `Backend did not return a job ID. Response: ${JSON.stringify(data)}`,
        );
      }

      setJobId(data.job_id);

      setQueuePosition(
        data.queue_position ??
          null,
      );

      localStorage.setItem(
        ACTIVE_JOB_KEY,
        JSON.stringify({
          jobId: data.job_id,
        }),
      );

      setStage("Queued");
      setProgress(1);
      setStatusMessage(
        data.queue_position &&
          data.queue_position > 1
          ? `Queued behind ${data.queue_position - 1} generation(s)...`
          : "Your video is next on the GPU...",
      );

      pollStatus(
        data.job_id,
      );
    } catch (err) {
      console.error(
        "GENERATION ERROR:",
        err,
      );

      setIsGenerating(false);
      setStage("Error");
      setProgress(0);

      setError(
        err instanceof Error
          ? err.message
          : "Something went wrong while starting generation.",
      );

      setStatusMessage("");
    }
  };

  const backendStatus =
    systemStatus?.backend?.status ||
    "unknown";

  const comfyStatus =
    systemStatus?.comfyui?.status ||
    "unknown";

  const ffmpegStatus =
    systemStatus?.ffmpeg?.status ||
    "unknown";

  const storage =
    systemStatus?.storage;

  const generation =
    systemStatus?.generation;

  const resolutionWarning =
    resolution === "1024x576";

  const heavyGeneration =
    resolution === "1024x576" &&
    (quality === "high" ||
      duration === 8);

  const overallStatus =
    systemStatus?.status === "ok"
      ? "ONLINE"
      : "DEGRADED";

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">
            ✦
          </span>
          <span>
            MotionForge
          </span>
        </div>

        <div className="topbar-right">
          <span className="engine-badge">
            WAN · COMFYUI
          </span>

          <span
            className={`status-pill ${
              overallStatus === "ONLINE"
                ? "online"
                : "warning"
            }`}
          >
            ● {overallStatus}
          </span>
        </div>
      </header>

      <main className="container">
        <section className="hero">
          <div className="eyebrow">
            LOCAL AI VIDEO STUDIO
          </div>

          <h1>
            Turn ideas into
            <span>
              cinematic motion.
            </span>
          </h1>

          <p>
            Describe a scene. Choose your
            settings. Let your local GPU
            create it.
          </p>
        </section>

        <section className="system-panel">
          <div className="system-title">
            <div>
              <span className="section-kicker">
                SYSTEM
              </span>
              <h2>
                Local generation status
              </h2>
            </div>

            <span className="system-queue">
              {generation?.queue_size ?? 0}{" "}
              queued
            </span>
          </div>

          <div className="system-grid">
            <div className="system-item">
              <span>Backend</span>
              <strong
                className={
                  backendStatus === "ok"
                    ? "ok"
                    : "bad"
                }
              >
                {backendStatus === "ok"
                  ? "Online"
                  : "Offline"}
              </strong>
            </div>

            <div className="system-item">
              <span>ComfyUI</span>
              <strong
                className={
                  comfyStatus ===
                  "connected"
                    ? "ok"
                    : "bad"
                }
              >
                {comfyStatus ===
                "connected"
                  ? "Connected"
                  : "Unavailable"}
              </strong>
            </div>

            <div className="system-item">
              <span>FFmpeg</span>
              <strong
                className={
                  ffmpegStatus ===
                  "available"
                    ? "ok"
                    : "bad"
                }
              >
                {ffmpegStatus ===
                "available"
                  ? "Ready"
                  : "Missing"}
              </strong>
            </div>

            <div className="system-item">
              <span>Storage</span>
              <strong>
                {formatBytes(
                  storage?.generated_bytes,
                )}
              </strong>
            </div>
          </div>

          {systemError && (
            <div className="system-error">
              {systemError}
            </div>
          )}
        </section>

        <section className="generator-card">
          <div className="section-heading">
            <div>
              <span className="section-kicker">
                CREATE
              </span>

              <h2>
                Describe your video
              </h2>
            </div>

            <span className="counter">
              {prompt.length} / 2000
            </span>
          </div>

          <textarea
            id="prompt"
            value={prompt}
            onChange={(event) =>
              setPrompt(
                event.target.value.slice(
                  0,
                  2000,
                ),
              )
            }
            placeholder="Describe the scene, camera movement, lighting, atmosphere and subject..."
            rows={8}
            disabled={isGenerating}
          />

          <div className="preset-header">
            <span>
              Try an example
            </span>
          </div>

          <div className="preset-row">
            {PROMPT_PRESETS.map(
              (preset) => (
                <button
                  key={preset.name}
                  className="preset-chip"
                  onClick={() =>
                    selectPreset(
                      preset.prompt,
                    )
                  }
                  disabled={
                    isGenerating
                  }
                >
                  {preset.name}
                </button>
              ),
            )}
          </div>

          <div className="settings-grid">
            <div className="setting">
              <label htmlFor="resolution">
                Resolution
              </label>

              <select
                id="resolution"
                value={resolution}
                onChange={(event) =>
                  setResolution(
                    event.target.value as Resolution,
                  )
                }
                disabled={isGenerating}
              >
                <option value="512x288">
                  512 × 288
                </option>

                <option value="768x432">
                  768 × 432
                </option>

                <option value="1024x576">
                  1024 × 576
                </option>
              </select>
            </div>

            <div className="setting">
              <label htmlFor="duration">
                Duration
              </label>

              <select
                id="duration"
                value={duration}
                onChange={(event) =>
                  setDuration(
                    Number(
                      event.target.value,
                    ) as Duration,
                  )
                }
                disabled={isGenerating}
              >
                <option value={3}>
                  3 seconds
                </option>

                <option value={5}>
                  5 seconds
                </option>

                <option value={8}>
                  8 seconds
                </option>
              </select>
            </div>

            <div className="setting">
              <label htmlFor="quality">
                Quality
              </label>

              <select
                id="quality"
                value={quality}
                onChange={(event) =>
                  setQuality(
                    event.target.value as Quality,
                  )
                }
                disabled={isGenerating}
              >
                <option value="fast">
                  Fast · 15 steps
                </option>

                <option value="standard">
                  Standard · 25 steps
                </option>

                <option value="high">
                  High · 35 steps
                </option>
              </select>
            </div>
          </div>

          <div className="settings-summary">
            <span>
              {formatResolution(
                resolution,
              )}
            </span>

            <span>•</span>

            <span>
              {duration}s
            </span>

            <span>•</span>

            <span>
              {formatQuality(
                quality,
              )}
            </span>

            <span>•</span>

            <span>
              {duration === 3
                ? 49
                : duration === 5
                  ? 81
                  : 129}{" "}
              frames
            </span>
          </div>

          {resolutionWarning && (
            <div
              className={`performance-warning ${
                heavyGeneration
                  ? "heavy"
                  : ""
              }`}
            >
              <strong>
                {heavyGeneration
                  ? "⚠ Heavy GPU workload"
                  : "⚠ Higher GPU usage"}
              </strong>

              <span>
                1024 × 576 uses substantially
                more VRAM and may take longer
                on a 6 GB GPU.
              </span>
            </div>
          )}

          <div className="button-row">
            <button
              className="generate-button"
              onClick={
                generateVideo
              }
              disabled={
                isGenerating
              }
            >
              {isGenerating ? (
                <>
                  <span className="spinner" />
                  Generating...
                </>
              ) : (
                <>
                  ✦ Generate Video
                </>
              )}
            </button>

            <button
              className="clear-button"
              onClick={
                clearVideo
              }
              disabled={
                isGenerating
              }
            >
              Clear
            </button>
          </div>

          {isGenerating && (
            <div className="generation-status">
              <div className="status-top">
                <div>
                  <span className="status-label">
                    {stage}
                  </span>

                  <strong>
                    {statusMessage}
                  </strong>

                  {node && (
                    <small>
                      Active workflow node:{" "}
                      {node}
                    </small>
                  )}

                  {queuePosition &&
                    stage.toLowerCase().includes(
                      "queue",
                    ) && (
                      <small>
                        Queue position:{" "}
                        {queuePosition}
                      </small>
                    )}
                </div>

                <span className="status-percent">
                  {Math.round(
                    progress,
                  )}
                  %
                </span>
              </div>

              <div className="progress-track">
                <div
                  className="progress-fill"
                  style={{
                    width: `${Math.max(
                      2,
                      progress,
                    )}%`,
                  }}
                />
              </div>

              <div className="stage-row">
                <span
                  className={
                    progress >= 4
                      ? "stage active"
                      : "stage"
                  }
                >
                  <b>1</b>
                  Queue
                </span>

                <span
                  className={
                    progress >= 5
                      ? "stage active"
                      : "stage"
                  }
                >
                  <b>2</b>
                  Generate
                </span>

                <span
                  className={
                    progress >= 92
                      ? "stage active"
                      : "stage"
                  }
                >
                  <b>3</b>
                  Convert
                </span>

                <span
                  className={
                    progress >= 99
                      ? "stage active"
                      : "stage"
                  }
                >
                  <b>4</b>
                  Preview
                </span>

                <span
                  className={
                    progress >= 100
                      ? "stage active"
                      : "stage"
                  }
                >
                  <b>5</b>
                  Ready
                </span>
              </div>
            </div>
          )}

          {error && (
            <div className="error-message">
              <strong>
                Generation error
              </strong>

              <span>
                {error}
              </span>
            </div>
          )}
        </section>

        {videoUrl && (
          <section className="result-section">
            <div className="result-header">
              <div>
                <span className="section-kicker">
                  GENERATED RESULT
                </span>

                <h2>
                  Your video is ready
                </h2>
              </div>

              <span className="ready-badge">
                ● READY
              </span>
            </div>

            <div className="video-card">
              <video
                className="video-player"
                src={videoUrl}
                poster={
                  thumbnailUrl ||
                  undefined
                }
                controls
                autoPlay
                loop
                playsInline
              />

              <div className="video-info">
                <div>
                  <span>
                    {formatResolution(
                      resolution,
                    )}
                  </span>

                  <span>•</span>

                  <span>
                    {duration}s
                  </span>

                  <span>•</span>

                  <span>
                    {formatQuality(
                      quality,
                    )}
                  </span>
                </div>
              </div>

              <div className="video-actions">
                <a
                  className="download-button"
                  href={videoUrl}
                  download="motionforge-video.mp4"
                >
                  ↓ Download MP4
                </a>

                <button
                  className="another-button"
                  onClick={
                    generateAnother
                  }
                >
                  ✦ Generate Another
                </button>
              </div>
            </div>
          </section>
        )}

        {history.length > 0 && (
          <section className="history-section">
            <div className="history-header">
              <div>
                <span className="section-kicker">
                  YOUR LIBRARY
                </span>

                <h2>
                  Recent Generations
                </h2>
              </div>

              <button
                className="clear-history-button"
                onClick={
                  clearHistory
                }
              >
                Clear History
              </button>
            </div>

            <div className="history-grid">
              {history.map(
                (item) => (
                  <article
                    className="history-card"
                    key={item.id}
                  >
                    <button
                      className="history-preview"
                      onClick={() =>
                        playHistoryItem(
                          item,
                        )
                      }
                    >
                      {item.thumbnailUrl ? (
                        <img
                          src={absoluteUrl(
                            item.thumbnailUrl,
                          ) || ""}
                          alt=""
                        />
                      ) : (
                        <video
                          src={
                            item.videoUrl
                          }
                          muted
                          playsInline
                          preload="metadata"
                        />
                      )}

                      <span className="history-play">
                        ▶
                      </span>
                    </button>

                    <div className="history-content">
                      <p className="history-prompt">
                        {item.prompt}
                      </p>

                      <div className="history-meta">
                        <span>
                          {formatResolution(
                            item.resolution,
                          )}
                        </span>

                        <span>
                          {item.duration}s
                        </span>

                        <span>
                          {formatQuality(
                            item.quality,
                          )}
                        </span>

                        <span>
                          {formatHistoryDate(
                            item.createdAt,
                          )}
                        </span>
                      </div>

                      <div className="history-actions">
                        <button
                          className="history-play-button"
                          onClick={() =>
                            playHistoryItem(
                              item,
                            )
                          }
                        >
                          ▶ Play
                        </button>

                        <a
                          className="history-download-button"
                          href={
                            absoluteUrl(
                              item.videoUrl,
                            ) || "#"
                          }
                          download="motionforge-video.mp4"
                        >
                          ↓ Download
                        </a>

                        <button
                          className="history-delete-button"
                          onClick={() =>
                            deleteHistoryItem(
                              item.id,
                            )
                          }
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                  </article>
                ),
              )}
            </div>
          </section>
        )}

        <footer>
          <span>
            MotionForge
          </span>

          <span>
            Local AI · ComfyUI · Wan
          </span>
        </footer>
      </main>
    </div>
  );
}

export default App;
