"""Latency report aggregation (Sprint 5, T5.1)."""

from __future__ import annotations

import json

from pincer.voice.latency_report import build_latency_report, read_turn_records


def _record(call: str, turn: int, total: float, **stages: float) -> dict:
    return {"call_sid": call, "turn": turn, "engine": "fake", "total_ms": total, **stages}


class TestReadTurnRecords:
    def test_missing_file_is_empty(self, tmp_path):
        assert read_turn_records(tmp_path / "nope.jsonl") == []

    def test_last_n_calls_kept(self, tmp_path):
        path = tmp_path / "lat.jsonl"
        lines = []
        for i in range(5):
            lines.append(json.dumps(_record(f"CA{i}", 1, 1000.0 + i)))
            lines.append(json.dumps(_record(f"CA{i}", 2, 900.0 + i)))
        path.write_text("\n".join(lines))

        records = read_turn_records(path, last_calls=2)
        assert {r["call_sid"] for r in records} == {"CA3", "CA4"}
        assert len(records) == 4  # both turns of each kept

    def test_garbage_lines_skipped(self, tmp_path):
        path = tmp_path / "lat.jsonl"
        path.write_text('not json\n{"no_sid": 1}\n' + json.dumps(_record("CA1", 1, 800.0)))
        records = read_turn_records(path)
        assert len(records) == 1


class TestBuildReport:
    def test_percentiles_per_stage(self, tmp_path):
        records = [
            _record("CA1", 1, 1000.0, llm_first_token_ms=400.0, first_dispatch_ms=600.0),
            _record("CA1", 2, 1200.0, llm_first_token_ms=500.0, first_dispatch_ms=700.0),
            _record("CA2", 1, 800.0, llm_first_token_ms=300.0, first_dispatch_ms=500.0),
        ]
        report = build_latency_report(records)
        assert report["turns"] == 3
        assert report["calls"] == 2
        assert report["engines"] == ["fake"]
        assert report["stages"]["total_ms"]["p50"] == 1000.0
        assert report["stages"]["total_ms"]["n"] == 3
        assert report["stages"]["llm_first_token_ms"]["p50"] == 400.0
        # stage ordering: pipeline order first
        keys = list(report["stages"].keys())
        assert keys.index("llm_first_token_ms") < keys.index("first_dispatch_ms") < keys.index("total_ms")

    def test_p95_interpolates(self):
        records = [_record("CA1", i, float(v)) for i, v in enumerate([100, 200, 300, 400, 1000])]
        report = build_latency_report(records)
        assert report["stages"]["total_ms"]["p50"] == 300.0
        assert report["stages"]["total_ms"]["p95"] > 800.0


class TestBuildModelReport:
    def _records(self):
        from pincer.voice.latency_report import build_model_report  # noqa: F401  (import smoke)

        return [
            {**_record("CA1", 1, 2400.0, llm_first_token_ms=2000.0), "turn_model": "claude-sonnet-4-5"},
            {**_record("CA1", 2, 2200.0, llm_first_token_ms=1800.0), "turn_model": "claude-sonnet-4-5"},
            {**_record("CA2", 1, 1500.0, llm_first_token_ms=1200.0), "turn_model": "claude-haiku-4-5"},
            {**_record("CA3", 1, 1400.0, llm_first_token_ms=1100.0), "turn_model": "claude-haiku-4-5"},
            {**_record("CA3", 2, 1700.0, llm_first_token_ms=1300.0, error=True), "turn_model": "claude-haiku-4-5"},
            _record("CA0", 1, 3000.0),  # legacy record, no turn_model
        ]

    def test_groups_by_model_sorted_by_total_p50(self):
        from pincer.voice.latency_report import build_model_report

        rows = build_model_report(self._records())
        assert [r["model"] for r in rows] == ["claude-haiku-4-5", "claude-sonnet-4-5", "unknown"]
        haiku = rows[0]
        assert haiku["turns"] == 3
        assert haiku["calls"] == 2
        assert haiku["errors"] == 1
        assert haiku["stages"]["total_ms"]["p50"] == 1500.0
        assert haiku["stages"]["llm_first_token_ms"]["n"] == 3
        sonnet = rows[1]
        assert sonnet["turns"] == 2 and sonnet["calls"] == 1 and sonnet["errors"] == 0
        assert sonnet["stages"]["total_ms"]["p50"] == 2300.0
        unknown = rows[2]
        assert unknown["turns"] == 1
        assert "llm_first_token_ms" not in unknown["stages"]

    def test_sort_by_name(self):
        from pincer.voice.latency_report import build_model_report

        rows = build_model_report(self._records(), sort="name")
        assert [r["model"] for r in rows] == ["claude-haiku-4-5", "claude-sonnet-4-5", "unknown"]
        rows = build_model_report([{**_record("CA1", 1, 100.0), "turn_model": "zeta"}, *self._records()], sort="name")
        assert rows[-1]["model"] == "zeta"

    def test_models_without_total_sort_last(self):
        from pincer.voice.latency_report import build_model_report

        records = [
            {"call_sid": "CA1", "turn": 1, "turn_model": "broken", "prep_ms": 5.0},
            {**_record("CA2", 1, 900.0), "turn_model": "fast"},
        ]
        rows = build_model_report(records)
        assert [r["model"] for r in rows] == ["fast", "broken"]

    def test_invalid_sort_rejected(self):
        import pytest

        from pincer.voice.latency_report import build_model_report

        with pytest.raises(ValueError):
            build_model_report([], sort="p99")

    def test_empty(self):
        from pincer.voice.latency_report import build_model_report

        assert build_model_report([]) == []


class TestLatencyModelCli:
    def test_table_and_json(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from pincer.cli import app

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        path = log_dir / "voice_latency.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    {**_record("CA1", 1, 2400.0, llm_first_token_ms=2000.0), "turn_model": "claude-sonnet-4-5"},
                    {**_record("CA2", 1, 1500.0, llm_first_token_ms=1200.0), "turn_model": "claude-haiku-4-5"},
                ]
            )
        )

        class _Settings:
            data_dir = tmp_path

        import pincer.config as config_mod

        monkeypatch.setattr(config_mod, "get_settings_relaxed", lambda: _Settings())

        runner = CliRunner()
        result = runner.invoke(app, ["voice", "latency-model"])
        assert result.exit_code == 0, result.output
        out = result.output
        assert "claude-haiku-4-5" in out and "claude-sonnet-4-5" in out
        assert out.index("claude-haiku-4-5") < out.index("claude-sonnet-4-5")

        result = runner.invoke(app, ["voice", "latency-model", "--json", "--sort", "name"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert [r["model"] for r in rows] == ["claude-haiku-4-5", "claude-sonnet-4-5"]

        result = runner.invoke(app, ["voice", "latency-model", "--sort", "p99"])
        assert result.exit_code == 2

    def test_no_records(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from pincer.cli import app

        class _Settings:
            data_dir = tmp_path

        import pincer.config as config_mod

        monkeypatch.setattr(config_mod, "get_settings_relaxed", lambda: _Settings())
        result = CliRunner().invoke(app, ["voice", "latency-model"])
        assert result.exit_code == 1
