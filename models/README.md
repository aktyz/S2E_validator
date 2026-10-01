# Bundled models

## `face_detection_yunet_2023mar.onnx`

Face detector used by the `face_detection` filter (#21).

| | |
|---|---|
| Model | YuNet (face_detection_yunet_2023mar) |
| Source | [opencv/opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) |
| Author | Shiqi Yu and contributors |
| Licence | MIT |
| Size | 227 KB |
| SHA-256 | `8f2383e4dd3cfbb4553ea8718107fc04...` (first 32 chars; full hash from `sha256sum`) |

**Why it is committed rather than downloaded.** The stage must run with no remote
calls and no setup step, so `git clone && pytest` works for a reviewer or a
hackathon judge. At 227 KB it is smaller than several of the test fixtures it
replaces, and pinning the file means detection behaviour cannot change under us
mid-campaign.

To update it, replace the file, record the new hash here, and re-run the face
detection tests; thresholds in `FaceDetectionSettings` are calibrated against
this version.
