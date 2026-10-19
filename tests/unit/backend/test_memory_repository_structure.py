from CAD2BIM_backend.infrastructure.memory import InMemoryControlPlaneRepository


def test_composed_repository_uses_one_shared_state_and_lock():
    repository = InMemoryControlPlaneRepository()

    assert repository._lock is repository._state.lock
    assert repository._tasks is repository._state.tasks
    assert repository._runs is repository._state.runs
    assert repository._artifacts is repository._state.artifacts
    assert repository._state.direct_sessions is not None
    assert repository._state.data_packages is not None
    assert repository._state.critical_operations is not None
