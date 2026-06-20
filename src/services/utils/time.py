import time


class Timer:
    def __init__(self):
        self.start_times = []

    def start(self):
        self.start_times.append(time.time())

    def defaultStart(self, timeArray=None):
        if timeArray is None:
            timeArray = []
        self.start_times.extend(timeArray)

    def getTime(self):
        return self.start_times

    def stop(self, label=""):
        if not self.start_times:
            raise Exception("Timer was not started")
        start_time = self.start_times.pop()
        elapsed_time = time.time() - start_time
        return elapsed_time


timer_obj = Timer()


# Thresholds in seconds for slow-call warnings
SLOW_CALL_THRESHOLDS = {
    "redis": 0.3,        # 300ms
    "mongo": 0.5,        # 500ms
    "pg": 0.5,           # 500ms
    "pre_function": 50.0, # 50s
    "openai_batch": 10.0, # 10s
    "batch_retrieve": 3.0, # 3s
}


def log_slow_call(label: str, elapsed: float, threshold: float) -> None:
    """Prints a warning only when elapsed exceeds threshold (seconds)."""
    if elapsed > threshold:
        print(f"[SLOW] {label} took {elapsed * 1000:.1f}ms (threshold {threshold * 1000:.0f}ms)")
