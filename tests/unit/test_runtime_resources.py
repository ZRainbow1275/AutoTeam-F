import logging

from autoteam import runtime_resources


def test_collect_runtime_resource_snapshot_reads_cgroup_and_counts_browser_processes(tmp_path, monkeypatch):
    status = tmp_path / "status"
    memory_current = tmp_path / "memory.current"
    memory_max = tmp_path / "memory.max"
    pids_current = tmp_path / "pids.current"
    pids_max = tmp_path / "pids.max"

    status.write_text("Name:\tpython\nVmRSS:\t2048 kB\n", encoding="utf-8")
    memory_current.write_text("1048576\n", encoding="utf-8")
    memory_max.write_text("2097152\n", encoding="utf-8")
    pids_current.write_text("12\n", encoding="utf-8")
    pids_max.write_text("768\n", encoding="utf-8")

    monkeypatch.setattr(runtime_resources, "PROC_SELF_STATUS", status)
    monkeypatch.setattr(runtime_resources, "CGROUP_MEMORY_CURRENT", memory_current)
    monkeypatch.setattr(runtime_resources, "CGROUP_MEMORY_MAX", memory_max)
    monkeypatch.setattr(runtime_resources, "CGROUP_PIDS_CURRENT", pids_current)
    monkeypatch.setattr(runtime_resources, "CGROUP_PIDS_MAX", pids_max)
    monkeypatch.setattr(
        runtime_resources,
        "_process_rows",
        lambda: [
            "Z chrome [chrome] <defunct>",
            "S chrome --type=renderer",
            "S python -m autoteam",
            "S playwright driver",
        ],
    )

    snapshot = runtime_resources.collect_runtime_resource_snapshot()

    assert snapshot["rss_mb"] == 2.0
    assert snapshot["cgroup_memory_mb"] == 1.0
    assert snapshot["cgroup_memory_limit_mb"] == 2.0
    assert snapshot["cgroup_memory_usage_ratio"] == 0.5
    assert snapshot["pids_current"] == 12
    assert snapshot["pids_max"] == 768
    assert snapshot["browser_process_total"] == 3
    assert snapshot["browser_process_live"] == 2
    assert snapshot["browser_process_zombie"] == 1


def test_collect_runtime_resource_snapshot_gracefully_handles_missing_files(tmp_path, monkeypatch):
    missing = tmp_path / "missing"

    monkeypatch.setattr(runtime_resources, "PROC_SELF_STATUS", missing)
    monkeypatch.setattr(runtime_resources, "CGROUP_MEMORY_CURRENT", missing)
    monkeypatch.setattr(runtime_resources, "CGROUP_MEMORY_MAX", missing)
    monkeypatch.setattr(runtime_resources, "CGROUP_PIDS_CURRENT", missing)
    monkeypatch.setattr(runtime_resources, "CGROUP_PIDS_MAX", missing)
    monkeypatch.setattr(runtime_resources, "_process_rows", lambda: [])

    snapshot = runtime_resources.collect_runtime_resource_snapshot()

    assert snapshot["rss_mb"] is None
    assert snapshot["cgroup_memory_mb"] is None
    assert snapshot["cgroup_memory_limit_mb"] is None
    assert snapshot["cgroup_memory_usage_ratio"] is None
    assert snapshot["pids_current"] is None
    assert snapshot["pids_max"] is None
    assert snapshot["browser_process_total"] == 0
    assert snapshot["browser_process_live"] == 0
    assert snapshot["browser_process_zombie"] == 0


def test_log_runtime_resource_snapshot_warns_and_runs_gc(monkeypatch, caplog):
    collected = []

    monkeypatch.setenv("AUTOTEAM_MEMORY_WARN_RATIO", "0.80")
    monkeypatch.setenv("AUTOTEAM_ZOMBIE_WARN_THRESHOLD", "1")
    monkeypatch.setattr(runtime_resources.gc, "collect", lambda: collected.append(True) or 7)
    monkeypatch.setattr(
        runtime_resources,
        "collect_runtime_resource_snapshot",
        lambda: {
            "rss_mb": 100.0,
            "cgroup_memory_mb": 900.0,
            "cgroup_memory_limit_mb": 1000.0,
            "cgroup_memory_usage_ratio": 0.9,
            "pids_current": 50,
            "pids_max": 768,
            "browser_process_total": 3,
            "browser_process_live": 2,
            "browser_process_zombie": 1,
        },
    )

    logger = logging.getLogger("test_runtime_resources")
    with caplog.at_level(logging.WARNING):
        snapshot = runtime_resources.log_runtime_resource_snapshot(logger, label="unit")

    assert snapshot["cgroup_memory_usage_ratio"] == 0.9
    assert collected == [True]
    assert "gc.collect reclaimed 7 objects" in caplog.text
    assert "browser zombie processes=1" in caplog.text
