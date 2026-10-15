"""The Docker side of build containers: waiting, and giving up when Docker is gone."""

import pytest
import requests

from planner import otp_runtime


class FakeContainer:
    def __init__(self, waits, reload_error=None):
        self.waits = list(waits)
        self.reload_error = reload_error
        self.attrs = {'State': {'OOMKilled': False}}
        self.removed = False

    def wait(self, timeout):
        outcome = self.waits.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def reload(self):
        if self.reload_error:
            raise self.reload_error

    def logs(self, tail):
        return b'done'

    def remove(self, force):
        self.removed = True


def use_container(monkeypatch, container):
    class Containers:
        def run(self, image, **kwargs):
            return container

    class Client:
        containers = Containers()

    monkeypatch.setattr(otp_runtime, '_client', lambda: Client())


def test_long_build_keeps_waiting_and_ticking(monkeypatch, settings):
    container = FakeContainer([requests.exceptions.ReadTimeout(), {'StatusCode': 0}])
    use_container(monkeypatch, container)
    ticks = []

    result = otp_runtime.run_otp(
        '/host/dir', ['--build'], heap='1g', mem_limit='2g', on_tick=lambda: ticks.append(1)
    )

    assert (result.exit_code, ticks, container.removed) == (0, [1], True)


def test_docker_going_away_mid_build_stops_the_wait(monkeypatch, settings):
    container = FakeContainer(
        [requests.exceptions.ConnectionError()], reload_error=requests.exceptions.ConnectionError()
    )
    use_container(monkeypatch, container)

    with pytest.raises(otp_runtime.DockerUnavailable):
        otp_runtime.run_otp('/host/dir', ['--build'], heap='1g', mem_limit='2g')
