import {
  AbsoluteFill,
  Easing,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
} from "remotion";

const frames = [
  {
    src: "faces/chinese_face_01.png",
    title: "Frame 0015",
    note: "Outdoor close-up, sunglasses preserved",
  },
  {
    src: "faces/chinese_face_02.png",
    title: "Frame 0038",
    note: "Blue arch scene, singing pose preserved",
  },
  {
    src: "faces/chinese_face_03.png",
    title: "Frame 0065",
    note: "Microphone close-up, smile preserved",
  },
];

const sceneStyle: React.CSSProperties = {
  background:
    "radial-gradient(circle at 20% 18%, #b63b45 0, transparent 34%), linear-gradient(135deg, #10141f 0%, #1d2634 46%, #12141a 100%)",
  color: "#f7f3ea",
  fontFamily:
    "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
};

const safeArea: React.CSSProperties = {
  width: "100%",
  height: "100%",
  padding: "76px 92px",
  boxSizing: "border-box",
};

const fade = (frame: number, start: number, end: number) =>
  interpolate(frame, [start, end], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });

const TitleScene = () => {
  const frame = useCurrentFrame();

  return (
    <AbsoluteFill style={sceneStyle}>
      <div
        style={{
          ...safeArea,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          gap: 34,
          opacity: fade(frame, 0, 32),
        }}
      >
        <div style={{ fontSize: 34, color: "#ffcf70", fontWeight: 700 }}>
          watch skill proof
        </div>
        <div
          style={{
            fontSize: 86,
            lineHeight: 1.02,
            fontWeight: 850,
            maxWidth: 980,
          }}
        >
          Video parsed, faces edited, then rebuilt as a new video.
        </div>
        <div style={{ fontSize: 36, lineHeight: 1.28, maxWidth: 920 }}>
          Source: YouTube URL. Output: captions, 100 extracted frames, three
          edited face stills.
        </div>
      </div>
    </AbsoluteFill>
  );
};

const FaceScene = ({ index }: { index: number }) => {
  const frame = useCurrentFrame();
  const item = frames[index];
  const progress = fade(frame, 0, 28);

  return (
    <AbsoluteFill style={sceneStyle}>
      <div
        style={{
          ...safeArea,
          display: "grid",
          gridTemplateColumns: "1.25fr 0.9fr",
          alignItems: "center",
          gap: 58,
        }}
      >
        <div
          style={{
            opacity: progress,
            scale: interpolate(frame, [0, 110], [0.98, 1.04], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            }),
            borderRadius: 18,
            overflow: "hidden",
            boxShadow: "0 30px 80px rgba(0,0,0,0.42)",
            background: "#000",
          }}
        >
          <Img
            src={staticFile(item.src)}
            style={{
              width: "100%",
              display: "block",
            }}
          />
        </div>
        <div
          style={{
            opacity: fade(frame, 10, 38),
            display: "flex",
            flexDirection: "column",
            gap: 24,
          }}
        >
          <div style={{ fontSize: 32, color: "#ffcf70", fontWeight: 750 }}>
            Chinese-face edit {index + 1}/3
          </div>
          <div style={{ fontSize: 72, lineHeight: 1.04, fontWeight: 850 }}>
            {item.title}
          </div>
          <div style={{ fontSize: 36, lineHeight: 1.25 }}>{item.note}</div>
          <div
            style={{
              marginTop: 18,
              height: 8,
              width: 260,
              borderRadius: 999,
              background: "#ffcf70",
            }}
          />
        </div>
      </div>
    </AbsoluteFill>
  );
};

const SummaryScene = () => {
  const frame = useCurrentFrame();

  const points = [
    "yt-dlp downloaded metadata, captions, video, and audio.",
    "ffmpeg selected 100 scene-aware frames.",
    "Three face frames were edited into new files only.",
    "Remotion assembled this saved MP4 in the project directory.",
  ];

  return (
    <AbsoluteFill style={sceneStyle}>
      <div
        style={{
          ...safeArea,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          gap: 36,
          opacity: fade(frame, 0, 28),
        }}
      >
        <div style={{ fontSize: 76, lineHeight: 1.03, fontWeight: 850 }}>
          Analysis turned into a new artifact.
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 22,
          }}
        >
          {points.map((point, index) => (
            <div
              key={point}
              style={{
                fontSize: 32,
                lineHeight: 1.22,
                padding: "26px 28px",
                borderRadius: 16,
                background: "rgba(255,255,255,0.09)",
                border: "1px solid rgba(255,255,255,0.14)",
                opacity: fade(frame, 12 + index * 10, 34 + index * 10),
              }}
            >
              {point}
            </div>
          ))}
        </div>
      </div>
    </AbsoluteFill>
  );
};

export const WatchChineseFaceVideo = () => {
  return (
    <AbsoluteFill>
      <Sequence durationInFrames={120}>
        <TitleScene />
      </Sequence>
      <Sequence from={120} durationInFrames={75}>
        <FaceScene index={0} />
      </Sequence>
      <Sequence from={195} durationInFrames={75}>
        <FaceScene index={1} />
      </Sequence>
      <Sequence from={270} durationInFrames={75}>
        <FaceScene index={2} />
      </Sequence>
      <Sequence from={345} durationInFrames={135}>
        <SummaryScene />
      </Sequence>
    </AbsoluteFill>
  );
};

const replacementShots = [
  {
    src: "faces/chinese_face_01.png",
    startFrame: 24 * 25,
    durationInFrames: 50,
  },
  {
    src: "faces/chinese_face_02.png",
    startFrame: 62 * 25,
    durationInFrames: 50,
  },
  {
    src: "faces/chinese_face_03.png",
    startFrame: 104 * 25,
    durationInFrames: 50,
  },
];

const ReplacementOverlay = ({ src }: { src: string }) => {
  const frame = useCurrentFrame();

  return (
    <AbsoluteFill
      style={{
        background: "#000",
        opacity: interpolate(frame, [0, 5, 45, 50], [0, 1, 1, 0], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        }),
      }}
    >
      <Img
        src={staticFile(src)}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
        }}
      />
    </AbsoluteFill>
  );
};

export const FullLengthFaceReplacement = () => {
  return (
    <AbsoluteFill style={{ background: "#000" }}>
      <OffthreadVideo
        src={staticFile("source/original_downloaded_video.mp4")}
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
        }}
      />
      {replacementShots.map((shot) => (
        <Sequence
          key={shot.src}
          from={shot.startFrame}
          durationInFrames={shot.durationInFrames}
        >
          <ReplacementOverlay src={shot.src} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
