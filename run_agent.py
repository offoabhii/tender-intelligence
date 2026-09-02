"""
Main pipeline entry point.
"""

import sys

from app.agent_runner import run_pipeline


if __name__ == "__main__":
    try:
        count = run_pipeline()
        print(f"\nPipeline finished. Verified tenders: {count}")

        # Zero verified tenders is a valid scan result.
        sys.exit(0)

    except Exception as error:
        print(f"\nPIPELINE FAILED: {error}")
        sys.exit(1)