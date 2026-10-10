"""Run roster retries in existing web workers; database locks serialize writes."""
def post_worker_init(worker):
    from school_sync.django_backend import start_worker
    start_worker()
