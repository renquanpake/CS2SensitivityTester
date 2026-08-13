import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from cs2_sensitivity_tester import (
    Benchmark,
    TrialResult,
    SessionStore,
    advance_search,
    angle_to_screen,
    append_history,
    build_blocks,
    cm_per_360,
    export_results_csv,
    fitts_throughput,
    generate_candidates,
    load_history,
    make_trials,
    mode_description,
    new_warmup_session,
    new_session,
    select_recommendation,
    session_progress,
    state_to_results,
    summarize_results,
    validate_positive_number,
)


class SensitivityTesterTests(unittest.TestCase):
    def test_candidates_cover_current_search_window(self):
        self.assertEqual(generate_candidates(2.0, 0.6, 0.1), [1.4, 2.0, 2.6])

    def test_cm_per_360_uses_cs2_yaw(self):
        self.assertAlmostEqual(cm_per_360(800, 1.0, 0.022), 51.9545, places=3)

    def test_crosshair_moves_in_the_same_direction_as_mouse_angles(self):
        center = angle_to_screen(0.0, 0.0, 1920, 1080)
        self.assertGreater(angle_to_screen(5.0, 0.0, 1920, 1080)[0], center[0])
        self.assertLess(angle_to_screen(-5.0, 0.0, 1920, 1080)[0], center[0])
        self.assertGreater(angle_to_screen(0.0, 5.0, 1920, 1080)[1], center[1])
        self.assertLess(angle_to_screen(0.0, -5.0, 1920, 1080)[1], center[1])

    def test_invalid_values_are_rejected(self):
        for value in ("0", "-1", "nan", "nope"):
            with self.assertRaises(ValueError):
                validate_positive_number(value, "value")

    def test_mode_description_sets_clear_time_and_trial_expectations(self):
        self.assertEqual(mode_description("quick"), "182 次目标（含 20 次热身），约 4 分钟。")
        self.assertEqual(mode_description("reliable"), "344 次目标（含 20 次热身），约 8 分钟。")

    def test_recommendation_requires_at_least_one_trial(self):
        with self.assertRaises(ValueError):
            select_recommendation(summarize_results([], [1.0]))

    def test_trial_generator_is_repeatable_and_balanced(self):
        first = make_trials(30, 42)
        second = make_trials(30, 42)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 30)
        self.assertTrue(all(trial.distance > 0 for trial in first))
        self.assertTrue(any(abs(trial.horizontal) > 30 for trial in first))
        self.assertTrue(any(abs(trial.vertical) > 20 for trial in first))

    def test_trial_generator_stays_in_view_on_ultrawide_screens(self):
        for trial in make_trials(100, 42, 21 / 9):
            x, y = angle_to_screen(trial.horizontal, trial.vertical, 2100, 900)
            self.assertGreaterEqual(x, 140)
            self.assertLessEqual(x, 1960)
            self.assertGreaterEqual(y, 60)
            self.assertLessEqual(y, 840)

    def test_mode_block_counts_and_order_are_reproducible(self):
        candidates = generate_candidates(1.0, 0.3, 0.05)
        self.assertEqual(len(build_blocks(candidates, 0, 1)), 3)
        self.assertEqual(build_blocks(candidates, 0, 1), build_blocks(candidates, 0, 1))

    def test_search_expands_at_edge_and_refines_in_middle(self):
        for winner, expected in ((1.3, [1.0, 1.3, 1.6]), (1.0, [0.85, 1.0, 1.15])):
            state = new_session(1.0, 800, 0.022, "quick")
            for candidate in state["rounds"][0]["candidates"]:
                result = TrialResult(candidate, 8.0, 2.0, True, 0.5, 3.0 if candidate == winner else 1.0, False)
                state["results"].append(asdict(result))
            state["block_index"] = len(state["blocks"])
            self.assertTrue(advance_search(state))
            self.assertEqual(state["rounds"][1]["candidates"], expected)

    def test_search_uses_older_samples_to_refine_an_edge(self):
        state = new_session(1.0, 800, 0.022, "quick")
        for candidate in state["rounds"][0]["candidates"]:
            score = 3.0 if candidate == 1.0 else 1.0
            state["results"].append(asdict(TrialResult(candidate, 8.0, 2.0, True, 0.5, score, False)))
        state["block_index"] = len(state["blocks"])
        advance_search(state)

        for candidate in state["rounds"][1]["candidates"]:
            score = 4.0 if candidate == 1.15 else 1.0
            state["results"].append(asdict(TrialResult(candidate, 8.0, 2.0, True, 0.5, score, False)))
        state["block_index"] = len(state["blocks"])
        advance_search(state)

        self.assertEqual(state["rounds"][2]["candidates"], [1.075, 1.15, 1.225])

    def test_search_marks_session_complete_after_final_round(self):
        state = new_session(1.0, 800, 0.022, "quick")
        state["rounds"] = [{"step": 0.1} for _ in range(state["search_rounds"])]
        self.assertFalse(advance_search(state))
        self.assertTrue(state["completed"])

    def test_miss_has_zero_throughput(self):
        self.assertEqual(fitts_throughput(8.0, 2.0, 0.5, False), 0.0)
        self.assertGreater(fitts_throughput(8.0, 2.0, 0.5, True), 0.0)

    def test_feedback_automatically_starts_the_next_trial(self):
        benchmark = Benchmark(new_session(1.0, 800, 0.022, "quick"), SessionStore())
        benchmark.phase = "feedback"
        benchmark.feedback_until = 0.0
        with patch.object(benchmark, "_start_trial") as start_trial:
            benchmark._update()
        start_trial.assert_called_once_with()

    def test_stable_candidate_wins_inside_two_percent_range(self):
        results = []
        for candidate, values in ((1.0, (10.0, 10.0)), (1.15, (10.1, 9.7))):
            for throughput in values:
                results.append(TrialResult(candidate, 8.0, 2.0, True, 0.5, throughput, False))
        summary = summarize_results(results, [1.0, 1.15])
        recommendation = select_recommendation(summary)
        self.assertEqual(recommendation["selected"], 1.0)
        self.assertEqual(recommendation["range"], (1.0, 1.15))

    def test_faster_candidate_wins_when_otherwise_identical(self):
        results = [
            TrialResult(1.0, 8.0, 2.0, True, 0.4, 2.0, False),
            TrialResult(1.0, 8.0, 2.0, True, 0.5, 2.0, False),
            TrialResult(1.15, 8.0, 2.0, True, 0.8, 2.0, False),
            TrialResult(1.15, 8.0, 2.0, True, 0.9, 2.0, False),
        ]
        summary = summarize_results(results, [1.0, 1.15])
        self.assertEqual(select_recommendation(summary)["selected"], 1.0)

    def test_faster_candidate_wins_with_equal_stdev_but_worse_throughput(self):
        results = [
            TrialResult(1.0, 8.0, 2.0, True, 0.4, 2.0, False),
            TrialResult(1.0, 8.0, 2.0, True, 0.4, 2.0, False),
            TrialResult(1.15, 8.0, 2.0, True, 0.8, 2.01, False),
            TrialResult(1.15, 8.0, 2.0, True, 0.8, 2.01, False),
        ]
        summary = summarize_results(results, [1.0, 1.15])
        self.assertEqual(select_recommendation(summary)["selected"], 1.0)

    def _completed_state(self) -> dict:
        state = new_session(1.0, 800, 0.022, "quick")
        state["completed"] = True
        state["warmup_done"] = True
        results = []
        for candidate in state["candidates"]:
            results.append(asdict(TrialResult(candidate, 8.0, 2.0, True, 0.5, 2.0, False)))
        state["results"] = results
        return state

    def test_history_appends_and_loads(self):
        state = self._completed_state()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            self.assertEqual(load_history(path), [])
            append_history(path, state)
            append_history(path, state)
            entries = load_history(path)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[-1]["recommended"], select_recommendation(
                summarize_results(state_to_results(state["results"]), state["candidates"])
            )["selected"])
            self.assertTrue(entries[-1]["timestamp"])
            self.assertIn("hit_rate", entries[-1])

    def test_history_ignores_empty_or_malformed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(load_history(path), [])
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_history(path), [])

    def test_history_is_limited_to_recent_entries(self):
        state = self._completed_state()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            for _ in range(25):
                append_history(path, state)
            entries = load_history(path)
            self.assertEqual(len(entries), 20)

    def test_export_results_csv(self):
        state = self._completed_state()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.csv"
            export_results_csv(path, state)
            lines = path.read_text(encoding="utf-8-sig").strip().splitlines()
            self.assertEqual(lines[0], "candidate,trials,hit_rate,mean_throughput,stdev_throughput,median_time")
            self.assertEqual(len(lines), 1 + len(state["candidates"]))

    def test_session_round_trip(self):
        state = new_session(1.0, 800, 0.022, "quick")
        self.assertEqual(state["active_results"], [])
        self.assertEqual(state["version"], 2)
        self.assertEqual(len(state["blocks"]), 3)
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "session.json")
            store.save(state)
            loaded = store.load()
            self.assertEqual(loaded["candidates"], state["candidates"])
            self.assertEqual(json.loads(store.path.read_text(encoding="utf-8"))["mode"], "quick")

            store.clear()
            self.assertFalse(store.path.exists())

    def test_warmup_session_is_fixed_and_not_a_search(self):
        state = new_warmup_session(1.25, 800, 0.022)
        self.assertTrue(state["practice"])
        self.assertEqual(state["mode"], "warmup")
        self.assertEqual(state["base"], 1.25)
        self.assertNotIn("blocks", state)

    def test_session_progress_counts_warmup_blocks_and_active_trials(self):
        state = new_session(1.0, 800, 0.022, "quick")
        self.assertEqual(session_progress(state), (0, 182))
        state["active_results"] = [{}] * 4
        self.assertEqual(session_progress(state), (4, 182))
        state["warmup_done"] = True
        state["block_index"] = 2
        state["active_results"] = [{}] * 3
        self.assertEqual(session_progress(state), (59, 182))
        state["completed"] = True
        self.assertEqual(session_progress(state), (182, 182))

    def test_reliable_session_has_four_search_rounds(self):
        state = new_session(1.0, 800, 0.022, "reliable")
        self.assertEqual(state["search_rounds"], 4)
        self.assertEqual(session_progress(state), (0, 344))

    def test_record_trial_clamps_elapsed_and_keeps_warmup_out_of_results(self):
        benchmark = Benchmark(new_warmup_session(1.0, 800, 0.022), None)
        benchmark.current_trial = make_trials(1, 42)[0]
        benchmark.current_candidate = 1.0
        benchmark.current_warmup = True
        benchmark._record_trial(True, 99.0)
        result = benchmark.state["active_results"][0]
        self.assertEqual(result["elapsed"], 1.5)
        self.assertTrue(result["warmup"])
        benchmark._finish_block()
        self.assertEqual(len(benchmark.state["results"]), 1)
        self.assertEqual(benchmark.state["active_results"], [])
        self.assertTrue(benchmark.state["completed"])


if __name__ == "__main__":
    unittest.main()
