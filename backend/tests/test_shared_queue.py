"""Several workers sharing one job queue (the multi-process deployment).

Each JobRunner here has its own worker id, as separate backend processes would;
they race over the same database. On SQLite this runs everywhere; CI's
"Backend on PostgreSQL" job runs it against PostgreSQL, the database meant for
several processes.
"""

import threading
from collections import Counter
from typing import Any

from fastapi.testclient import TestClient

from ai3d.workers import JobRunner

WORKERS = 4


def _queue_jobs(client: TestClient, make_image_project: Any, count: int) -> list[str]:
    project, ids = make_image_project(1, allow_cloud_upload=True)
    jobs = []
    for _ in range(count):
        response = client.post(
            f"/api/v1/projects/{project['id']}/generations",
            json={
                "inputs": [{"image_id": ids[0]}],
                "provider": "mock",
                "cloud_upload_consent": True,
            },
        )
        assert response.status_code == 201, response.text
        jobs.append(response.json()["id"])
    return jobs


def _workers(runner: JobRunner, count: int) -> list[JobRunner]:
    return [
        JobRunner(
            database=runner.database,
            storage=runner.storage,
            settings=runner.settings,
            providers=runner.providers,
            worker_id=f"test-worker-{n}",
        )
        for n in range(count)
    ]


def test_every_job_is_claimed_by_exactly_one_worker(
    client: TestClient, runner: JobRunner, make_image_project: Any
) -> None:
    jobs = _queue_jobs(client, make_image_project, 40)
    claimed: list[tuple[str, str]] = []
    start = threading.Barrier(WORKERS)
    lock = threading.Lock()

    def work(worker: JobRunner) -> None:
        start.wait()
        while (job_id := worker.claim_next()) is not None:
            with lock:
                claimed.append((str(job_id), worker.worker_id))

    threads = [threading.Thread(target=work, args=(w,)) for w in _workers(runner, WORKERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    counts = Counter(job for job, _ in claimed)
    assert sorted(counts) == sorted(jobs)
    assert set(counts.values()) == {1}  # nobody claimed a job twice
    for job_id in jobs:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        assert job["status"] == "running"
    assert len({worker for _, worker in claimed}) > 1  # the work really was shared


def test_two_workers_run_a_queue_to_the_end_once_each(
    client: TestClient, runner: JobRunner, make_image_project: Any
) -> None:
    jobs = _queue_jobs(client, make_image_project, 6)
    workers = _workers(runner, 2)
    done: list[int] = []
    threads = [
        threading.Thread(target=lambda w=w: done.append(w.run_until_idle())) for w in workers
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert sum(done) == len(jobs)
    project_id = client.get(f"/api/v1/jobs/{jobs[0]}").json()["project_id"]
    for job_id in jobs:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        assert job["status"] == "succeeded", job["error"]
        assert job["attempt"] == 1  # never run twice
    models = client.get(f"/api/v1/projects/{project_id}/models").json()["items"]
    assert len(models) == len(jobs)  # one model per job, none duplicated
