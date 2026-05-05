import os

GTWY_BASE_URL = os.getenv("GTWY_BASE_URL", "https://api.gtwy.ai")
GTWY_PAUTH_KEY = os.getenv("GTWY_PAUTH_KEY", "5873b66600c945016d8d677634b4ed69")

# Bridge IDs — set these to your actual bridge IDs
PLANNER_BRIDGE_ID = os.getenv("PLANNER_BRIDGE_ID", "68e88f800664896cba0f2456")
EXECUTOR_BRIDGE_ID = os.getenv("EXECUTOR_BRIDGE_ID", "68e88f800664896cba0f2456")

STATE_DIR = os.path.join(os.path.dirname(__file__), "state")
