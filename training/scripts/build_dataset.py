"""Build validated train/dev/test JSONL files from raw teacher trajectories."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.pipeline import DatasetBuilder, read_jsonl


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument(
        "--tool-schema",
        default="config/deepresearch_tools.v1.json",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--target-valid", type=int)
    parser.add_argument("--min-quality", type=float, default=0.75)
    args = parser.parse_args()

    builder = DatasetBuilder(
        tool_schema_path=args.tool_schema,
        min_quality=args.min_quality,
    )
    manifest = builder.build(
        read_jsonl(args.input),
        output_dir=args.output_dir,
        target_valid=args.target_valid,
    )
    print(manifest)


if __name__ == "__main__":
    main()
