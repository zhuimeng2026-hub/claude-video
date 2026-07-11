import "./index.css";
import { Composition } from "remotion";
import {
  FullLengthFaceReplacement,
  WatchChineseFaceVideo,
} from "./Composition";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="WatchChineseFaceVideo"
        component={WatchChineseFaceVideo}
        durationInFrames={480}
        fps={30}
        width={1280}
        height={720}
      />
      <Composition
        id="FullLengthFaceReplacement"
        component={FullLengthFaceReplacement}
        durationInFrames={5327}
        fps={25}
        width={1280}
        height={720}
      />
    </>
  );
};
