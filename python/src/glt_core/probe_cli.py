"""One-shot media probe adapter used by the desktop frontend."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from glt_core.media import MediaError, probe_media


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glt-worker probe")
    parser.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    try:
        media = probe_media(args.input)
    except MediaError as exc:
        print(
            json.dumps(
                {"type": "error", "code": exc.code, "message": str(exc)},
                ensure_ascii=True,
            )
        )
        return 2
    document = {
        "type": "result",
        "path": str(media.path),
        "format_name": media.format_name,
        "duration_us": media.duration_us,
        "audio_streams": [
            {
                "position": stream.position,
                "index": stream.index,
                "codec_name": stream.codec_name,
                "sample_rate": stream.sample_rate,
                "channels": stream.channels,
                "language": stream.language,
                "title": stream.title,
                "is_default": stream.is_default,
            }
            for stream in media.audio_streams
        ],
    }
    print(json.dumps(document, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
