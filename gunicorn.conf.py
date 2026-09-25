# Gunicorn settings shared by dev and prod (the command line in the Dockerfile adds bind/workers).
def child_exit(server, worker):
    """Drop an exited worker's live metrics (prometheus_client multiprocess mode)."""
    try:
        from prometheus_client import multiprocess
        multiprocess.mark_process_dead(worker.pid)
    except ImportError:
        pass
