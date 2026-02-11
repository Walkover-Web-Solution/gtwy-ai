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
