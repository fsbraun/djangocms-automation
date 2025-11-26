import threading
import uuid
from django.tasks.backends.base import BaseTaskBackend
from django.db import close_old_connections, connection


class ThreadBackend(BaseTaskBackend):
    """
    A task backend that executes each task in a separate thread.
    Properly manages Django database connections to prevent leaks.
    """

    def __init__(self, options):
        super().__init__(options)
        self._results = {}
        self._lock = threading.Lock()

    def enqueue(self, task, args, kwargs, priority=None):
        """
        Enqueue a task to run in a new thread.
        Returns a task ID that can be used to retrieve results.
        """
        task_id = str(uuid.uuid4())

        def run_task():
            try:
                # Close any old/stale connections before starting
                close_old_connections()

                # Run the task
                result = task(*args, **kwargs)

                with self._lock:
                    self._results[task_id] = {"status": "completed", "result": result}
            except Exception as e:
                with self._lock:
                    self._results[task_id] = {"status": "failed", "error": str(e)}
            finally:
                # CRITICAL: Close the connection when thread finishes
                # This prevents connection leaks
                connection.close()

        # Mark task as pending
        with self._lock:
            self._results[task_id] = {"status": "pending"}

        # Start thread with daemon=False to ensure task completes
        thread = threading.Thread(target=run_task, daemon=False)
        thread.start()

        return task_id

    def get_result(self, result_id, timeout=None):
        """
        Retrieve the result of a completed task.
        Note: This implementation stores results in memory.
        """
        with self._lock:
            task_data = self._results.get(result_id)

        if not task_data:
            raise ValueError(f"No task found with ID: {result_id}")

        if task_data["status"] == "pending":
            return None  # Task still running
        elif task_data["status"] == "completed":
            return task_data["result"]
        elif task_data["status"] == "failed":
            raise Exception(f"Task failed: {task_data['error']}")
