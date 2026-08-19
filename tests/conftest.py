# pyright: basic

import os
import sys
from pathlib import Path

# Add src/ to path so `import moderate` resolves without installing.
# __file__ is tests/conftest.py → parent is tests/ → parent is repo root → / src
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Must be set before moderate is imported anywhere.
# Module-level env reads happen on import; missing vars crash immediately.
os.environ.update({
    "GITHUB_TOKEN":       "test-token",
    "GITHUB_REPOSITORY":  "owner/repo",
    "GITHUB_ACTOR":       "testactor",
    "GITHUB_EVENT_NAME":  "discussion_comment",
    "DISCUSSION_NODE_ID": "D_test123",
    "DISCUSSION_AUTHOR":  "discussion-author",
    "COMMENT_BODY":       "",
})