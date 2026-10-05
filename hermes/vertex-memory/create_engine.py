#!/usr/bin/env python3
"""One-time setup: create a Vertex AI Agent Engine instance for Memory Bank.

The plugin needs a `reasoning_engine_id`. This script creates one and prints
the id to drop into your config. Requires:

    pip install "google-cloud-aiplatform>=1.111.0"
    gcloud auth application-default login   # or a service-account ADC

Usage:
    python create_engine.py --project PROJECT_ID --location us-central1
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", required=True, help="GCP project id")
    ap.add_argument("--location", default="us-central1", help="GCP region")
    args = ap.parse_args()

    try:
        import vertexai
    except ImportError:
        print('Install the SDK first:\n'
              '  pip install "google-cloud-aiplatform>=1.111.0"', file=sys.stderr)
        return 1

    client = vertexai.Client(project=args.project, location=args.location)
    print(f"Creating Agent Engine in {args.project}/{args.location} ...")
    agent_engine = client.agent_engines.create()
    name = agent_engine.api_resource.name  # projects/.../reasoningEngines/NNNN
    engine_id = name.rsplit("/", 1)[-1]

    print("\nAgent Engine created.")
    print(f"  resource: {name}")
    print(f"  reasoning_engine_id: {engine_id}\n")
    print("Add to $HERMES_HOME/vertex-memory/config.json (or the Memory panel of the "
          "Hermes admin dashboard):")
    print(f'  {{\n    "project_id": "{args.project}",\n'
          f'    "location": "{args.location}",\n'
          f'    "reasoning_engine_id": "{engine_id}",\n'
          f'    "scope_key": "user_id"\n  }}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
