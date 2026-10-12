"""Docker and HTTP access to OpenTripPlanner; the only planner module that talks to Docker.

Build and test containers follow the GTFS validator's hardening
(data_manager.tasks.validate_gtfs_feed_task): no network, the app's UID:GID,
no capabilities, bounded memory and processes. The serving OTP is the
docker-compose service ``otp``; it is only stopped and started from here.
"""

import threading
from dataclasses import dataclass

import docker
import requests
from django.conf import settings

OTP_HOME = '/var/opentripplanner'
BUILD_LABEL = 'odt.planner'
WAIT_POLL_SECONDS = 60
PIDS_LIMIT = 2048
LOG_TAIL_LINES = 80
HEALTH_TIMEOUT_SECONDS = 5
QUERY_TIMEOUT_SECONDS = 30
# Concurrent OTP queries per web process, and how long a request waits for a
# free slot before it is refused: one slow OTP must not tie up every thread.
# 3 gunicorn workers x 2 = 6 at once, which OTP with -Xmx6g served without
# stalls in the 2026-10-12 measurements (docs/deployment.md, section 19).
MAX_CONCURRENT_QUERIES = 2
QUERY_SLOT_WAIT_SECONDS = 1
_query_slots = threading.BoundedSemaphore(MAX_CONCURRENT_QUERIES)


class OtpUnavailable(Exception):
    pass


class DockerUnavailable(Exception):
    pass


class OtpBusy(OtpUnavailable):
    """Every query slot of this process is taken."""


class OtpQueryError(OtpUnavailable):
    """OTP answered, with GraphQL errors (possibly caused by the query's input)."""

    def __init__(self, messages: list[str]):
        super().__init__('; '.join(messages)[:500])
        self.messages = messages


@dataclass
class RunResult:
    exit_code: int
    oom: bool
    log_tail: str


def _client():
    return docker.from_env()


def run_otp(host_dir, args, *, heap, mem_limit, memswap_limit=None, mounts=None, on_tick=None):
    """Run an OTP build container on ``host_dir`` and wait for it.

    ``on_tick`` is called about once a minute while waiting, so a long build
    keeps its heartbeat fresh. ``mounts`` adds read-only files, host path to
    container path (the shared street graph).
    """
    volumes = {host_dir: {'bind': OTP_HOME, 'mode': 'rw'}}
    for host_path, container_path in (mounts or {}).items():
        volumes[host_path] = {'bind': container_path, 'mode': 'ro'}
    container = _client().containers.run(
        settings.PLANNER_OTP_IMAGE,
        command=list(args),
        detach=True,
        volumes=volumes,
        user=settings.PLANNER_CONTAINER_USER,
        environment={'JAVA_TOOL_OPTIONS': f'-Xmx{heap} -XX:+ExitOnOutOfMemoryError'},
        network_disabled=True,
        mem_limit=mem_limit,
        memswap_limit=memswap_limit or mem_limit,
        pids_limit=PIDS_LIMIT,
        cap_drop=['ALL'],
        security_opt=['no-new-privileges:true'],
        labels={BUILD_LABEL: 'build'},
    )
    try:
        while True:
            try:
                status = container.wait(timeout=WAIT_POLL_SECONDS)
                break
            except requests.exceptions.RequestException:
                # Usually the wait timed out; if Docker itself is gone, reload fails too.
                try:
                    container.reload()
                except (requests.exceptions.RequestException, docker.errors.DockerException) as exc:
                    raise DockerUnavailable(str(exc)) from exc
                if on_tick:
                    on_tick()
        container.reload()
        oom = bool(container.attrs.get('State', {}).get('OOMKilled'))
        logs = container.logs(tail=LOG_TAIL_LINES).decode('utf-8', errors='replace')
        return RunResult(exit_code=status.get('StatusCode', -1), oom=oom, log_tail=logs)
    finally:
        container.remove(force=True)


def remove_build_containers() -> None:
    """Remove build containers left behind by a worker that died mid-build."""
    for container in _client().containers.list(all=True, filters={'label': BUILD_LABEL}):
        container.remove(force=True)


def _server():
    return _client().containers.get(settings.PLANNER_OTP_CONTAINER)


def stop_server() -> None:
    _server().stop(timeout=30)


def start_server() -> None:
    _server().start()


def server_running() -> bool:
    return _server().status == 'running'


def is_healthy() -> bool:
    """True once OTP has loaded its graph and polled every realtime source once."""
    try:
        response = requests.get(
            f'{settings.PLANNER_OTP_URL}/otp/actuators/health', timeout=HEALTH_TIMEOUT_SECONDS
        )
    except requests.exceptions.RequestException:
        return False
    return response.status_code == 200


def query(
    document: str, variables: dict | None = None, timeout: int = QUERY_TIMEOUT_SECONDS
) -> dict:
    """Run a fixed GraphQL document against OTP's GTFS API; variables carry all input."""
    if not _query_slots.acquire(timeout=QUERY_SLOT_WAIT_SECONDS):
        raise OtpBusy
    try:
        response = requests.post(
            f'{settings.PLANNER_OTP_URL}/otp/gtfs/v1',
            json={'query': document, 'variables': variables or {}},
            # OTP gives up on its own before we stop waiting for it.
            headers={'OTPTimeout': str(timeout * 1000)},
            timeout=(3, timeout + 2),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        raise OtpUnavailable(str(exc)) from exc
    finally:
        _query_slots.release()
    if payload.get('errors'):
        raise OtpQueryError([str(error.get('message', error)) for error in payload['errors']])
    return payload['data']
