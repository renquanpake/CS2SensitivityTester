import csv
import ctypes
import json
import math
import os
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path


import pygame
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_NAME = "CS2 Sensitivity Tester"
STATE_DIR = Path(os.environ.get("APPDATA", Path.home())) / "CS2SensitivityTester"
STATE_PATH = STATE_DIR / "session.json"
HISTORY_PATH = STATE_DIR / "history.json"
HISTORY_LIMIT = 20
INITIAL_SEARCH_STEP_FACTOR = 0.30
MIN_SENSITIVITY_FACTOR = 0.05
SEARCH_ROUNDS = {"quick": 3, "reliable": 4}
TRIALS_PER_BLOCK = {"quick": 18, "reliable": 27}
TARGET_SIZES = (1.5, 3.0, 6.0)
TARGET_CENTER_EXCLUSION = 0.18
TRIAL_TIMEOUT_SECONDS = 1.5
WARMUP_TRIALS = 20
PRACTICE_TRIALS = 30
APP_BG = "#f2f4f3"
SURFACE = "#ffffff"
TEXT = "#17201c"
MUTED = "#5e6a64"
ACCENT = "#087a55"
ACCENT_DARK = "#066243"
BORDER = "#d8dedb"
WARNING = "#a75c00"
TUTORIAL_LINES = (
    "输入你现在的 CS2 灵敏度和鼠标 DPI。",
    "进入全屏后按空格键开始一组，移动准星并点击绿色目标。",
    "完成全部测试后，直接使用程序给出的推荐灵敏度。",
)


@dataclass
class Trial:
    distance: float
    size: float
    horizontal: float
    vertical: float


@dataclass
class TrialResult:
    candidate: float
    distance: float
    size: float
    hit: bool
    elapsed: float
    throughput: float
    warmup: bool


def validate_positive_number(value: str, label: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise ValueError(f"{label}必须是数字。") from error
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label}必须大于 0。")
    return number


def cm_per_360(dpi: float, sensitivity: float, yaw: float) -> float:
    return 2.54 * 360.0 / (dpi * sensitivity * yaw)


def generate_candidates(center: float, step: float, minimum: float) -> list[float]:
    values = {round(max(minimum, center - step), 6), round(center, 6), round(center + step, 6)}
    while len(values) < 3:
        values.add(round(max(values) + step, 6))
    return sorted(values)


def mode_description(mode: str) -> str:
    rounds = SEARCH_ROUNDS[mode]
    total = WARMUP_TRIALS + rounds * 3 * TRIALS_PER_BLOCK[mode]
    minutes = "约 4 分钟" if mode == "quick" else "约 8 分钟"
    return f"{total} 次目标（含 {WARMUP_TRIALS} 次热身），{minutes}。"


def fitts_throughput(distance: float, size: float, elapsed: float, hit: bool) -> float:
    if not hit or elapsed <= 0:
        return 0.0
    difficulty = math.log2(distance / size + 1.0)
    return difficulty / elapsed


def angle_to_screen(horizontal: float, vertical: float, width: int, height: int) -> tuple[int, int]:
    half_w = width / 2.0
    center = (width // 2, height // 2)
    return (
        center[0] + int(math.tan(math.radians(horizontal)) * half_w),
        center[1] + int(math.tan(math.radians(vertical)) * half_w),
    )


def make_trials(count: int, seed: int, aspect_ratio: float = 16.0 / 9.0) -> list[Trial]:
    rng = random.Random(seed)
    vertical_limit = 0.86 / aspect_ratio
    trials: list[Trial] = []
    for index in range(count):
        # Sample the visible field rather than a narrow horizontal angle band.
        while True:
            horizontal = math.degrees(math.atan(rng.uniform(-0.86, 0.86)))
            vertical = math.degrees(math.atan(rng.uniform(-vertical_limit, vertical_limit)))
            if max(abs(horizontal) / 45.0, abs(vertical) / 28.0) >= TARGET_CENTER_EXCLUSION:
                break
        trials.append(
            Trial(
                math.hypot(horizontal, vertical),
                TARGET_SIZES[index % len(TARGET_SIZES)],
                horizontal,
                vertical,
            )
        )
    rng.shuffle(trials)
    return trials


def build_blocks(candidates: list[float], round_index: int, seed: int) -> list[dict]:
    blocks = [{"candidate": candidate, "round": round_index} for candidate in candidates]
    random.Random(seed).shuffle(blocks)
    for point_index, block in enumerate(blocks):
        block["point"] = point_index
    return blocks


def summarize_results(results: list[TrialResult], candidates: list[float]) -> list[dict]:
    rows = []
    for candidate in candidates:
        candidate_results = [result for result in results if not result.warmup and result.candidate == candidate]
        throughputs = [result.throughput for result in candidate_results]
        hit_rate = sum(result.hit for result in candidate_results) / len(candidate_results) if candidate_results else 0.0
        rows.append(
            {
                "candidate": candidate,
                "trials": len(candidate_results),
                "mean_throughput": statistics.fmean(throughputs) if throughputs else 0.0,
                "stdev_throughput": statistics.pstdev(throughputs) if len(throughputs) > 1 else 0.0,
                "hit_rate": hit_rate,
                "median_time": statistics.median([result.elapsed for result in candidate_results if result.hit])
                if any(result.hit for result in candidate_results)
                else None,
            }
        )
    return rows


def select_recommendation(summary: list[dict]) -> dict:
    available = [row for row in summary if row["trials"]]
    if not available:
        raise ValueError("没有可用于推荐的试次结果。")
    best_score = max(row["mean_throughput"] for row in available)
    tolerance = best_score * 0.98 if best_score > 0 else 0.0
    close_rows = [row for row in available if row["mean_throughput"] >= tolerance]

    def median_time_key(row: dict) -> float:
        return row["median_time"] if row["median_time"] is not None else math.inf

    selected = min(
        close_rows,
        key=lambda row: (row["stdev_throughput"], median_time_key(row), -row["hit_rate"], -row["mean_throughput"]),
    )
    return {
        "selected": selected["candidate"],
        "range": (min(row["candidate"] for row in close_rows), max(row["candidate"] for row in close_rows)),
        "close_candidates": [row["candidate"] for row in close_rows],
    }


def state_to_results(items: list[dict]) -> list[TrialResult]:
    return [TrialResult(**item) for item in items]


def session_progress(state: dict) -> tuple[int, int]:
    mode = state.get("mode", "quick")
    rounds = state.get("search_rounds", SEARCH_ROUNDS[mode])
    trials_per_block = TRIALS_PER_BLOCK[mode]
    total = WARMUP_TRIALS + rounds * 3 * trials_per_block
    if state.get("completed"):
        return total, total
    active = len(state.get("active_results", []))
    if not state.get("warmup_done"):
        return min(active, WARMUP_TRIALS), total
    completed = WARMUP_TRIALS + state.get("block_index", 0) * trials_per_block + active
    return min(completed, total), total


def configure_theme(root: tk.Misc) -> None:
    root.configure(background=APP_BG)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", font=("Segoe UI", 10), background=APP_BG, foreground=TEXT)
    style.configure("App.TFrame", background=APP_BG)
    style.configure("Surface.TFrame", background=SURFACE, borderwidth=1, relief="solid")
    style.configure("Title.TLabel", background=APP_BG, foreground=TEXT, font=("Segoe UI Semibold", 22))
    style.configure("Subtitle.TLabel", background=APP_BG, foreground=MUTED, font=("Segoe UI", 10))
    style.configure("Section.TLabel", background=APP_BG, foreground=TEXT, font=("Segoe UI Semibold", 12))
    style.configure("SurfaceTitle.TLabel", background=SURFACE, foreground=TEXT, font=("Segoe UI Semibold", 11))
    style.configure("Surface.TLabel", background=SURFACE, foreground=TEXT)
    style.configure("Muted.TLabel", background=APP_BG, foreground=MUTED)
    style.configure("SurfaceMuted.TLabel", background=SURFACE, foreground=MUTED)
    style.configure("Result.TLabel", background=SURFACE, foreground=TEXT, font=("Segoe UI Semibold", 34))
    style.configure("Warning.TLabel", background=SURFACE, foreground=WARNING)
    style.configure("TEntry", fieldbackground=SURFACE, foreground=TEXT, padding=7, bordercolor=BORDER)
    style.configure("TRadiobutton", background=APP_BG, foreground=TEXT, padding=2)
    style.configure("Surface.TRadiobutton", background=SURFACE, foreground=TEXT, padding=2)
    style.configure("Primary.TButton", background=ACCENT, foreground="#ffffff", padding=(18, 10), borderwidth=0)
    style.map("Primary.TButton", background=[("active", ACCENT_DARK), ("pressed", ACCENT_DARK)])
    style.configure("Secondary.TButton", background=SURFACE, foreground=TEXT, padding=(14, 9), bordercolor=BORDER)
    style.map("Secondary.TButton", background=[("active", "#e8ecea")])
    style.configure("Help.TButton", background=SURFACE, foreground=TEXT, padding=(8, 7), bordercolor=BORDER)
    style.map("Help.TButton", background=[("active", "#e8ecea")])
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT, rowheight=26)
    style.configure("Treeview.Heading", background="#e8ecea", foreground=TEXT, padding=(4, 4))


def center_window(window: tk.Misc, width: int | None = None, height: int | None = None) -> None:
    window.update_idletasks()
    width = width or window.winfo_reqwidth()
    height = height or window.winfo_reqheight()
    x = max(0, (window.winfo_screenwidth() - width) // 2)
    y = max(0, (window.winfo_screenheight() - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def append_search_round(state: dict, center: float, step: float) -> None:
    round_index = len(state["rounds"])
    candidates = generate_candidates(center, step, state["base"] * MIN_SENSITIVITY_FACTOR)
    search_round = {
        "index": round_index,
        "center": center,
        "step": step,
        "candidates": candidates,
    }
    state["rounds"].append(search_round)
    for candidate in candidates:
        if candidate not in state["candidates"]:
            state["candidates"].append(candidate)
    state["blocks"].extend(build_blocks(candidates, round_index, state["seed"] + round_index))


def advance_search(state: dict) -> bool:
    if len(state["rounds"]) >= state["search_rounds"]:
        state["completed"] = True
        return False

    current_round = state["rounds"][-1]
    summary = summarize_results(state_to_results(state["results"]), state["candidates"])
    available = [row for row in summary if row["trials"]]
    best = max(
        available,
        key=lambda row: (row["mean_throughput"], row["hit_rate"], -row["stdev_throughput"]),
    )
    selected = best["candidate"]
    lower = [row["candidate"] for row in available if row["candidate"] < selected]
    upper = [row["candidate"] for row in available if row["candidate"] > selected]
    next_step = current_round["step"]
    if lower and upper:
        next_step = min(selected - max(lower), min(upper) - selected) / 2.0
    append_search_round(state, selected, next_step)
    return True


class SessionStore:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
        temporary_path.replace(self.path)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


def _read_mouse_registry() -> tuple[int, int]:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Mouse") as key:
            speed, _ = winreg.QueryValueEx(key, "MouseSpeed")
            threshold1, _ = winreg.QueryValueEx(key, "MouseThreshold1")
            return int(speed), int(threshold1)
    except (OSError, TypeError):
        return 0, 0


def mouse_acceleration_status() -> str:
    if sys.platform != "win32":
        return "unknown"
    speed, _threshold1 = _read_mouse_registry()
    return "enabled" if speed != 0 else "disabled"


def play_feedback_sound(hit: bool) -> None:
    if sys.platform != "win32":
        return
    try:
        import winsound

        winsound.Beep(880 if hit else 200, 60 if hit else 90)
    except OSError:
        pass


def load_history(path: Path = HISTORY_PATH) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def append_history(path: Path, state: dict) -> list[dict]:
    summary = summarize_results(state_to_results(state["results"]), state["candidates"])
    recommendation = select_recommendation(summary)
    selected_rows = [row for row in summary if row["candidate"] == recommendation["selected"]]
    row = selected_rows[0] if selected_rows else {}
    entries = load_history(path)
    entries.append(
        {
            "timestamp": time.strftime("%Y-%m-%d %H:%M"),
            "mode": state.get("mode", "quick"),
            "base": state["base"],
            "dpi": state["dpi"],
            "yaw": state["yaw"],
            "recommended": recommendation["selected"],
            "range": list(recommendation["range"]),
            "trials": len(state.get("results", [])),
            "hit_rate": row.get("hit_rate", 0.0),
            "median_time": row.get("median_time"),
        }
    )
    entries = entries[-HISTORY_LIMIT:]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(entries, file, ensure_ascii=False, indent=2)
    temporary_path.replace(path)
    return entries


def export_results_csv(path: Path, state: dict) -> None:
    summary = summarize_results(state_to_results(state["results"]), state["candidates"])
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(
            ["candidate", "trials", "hit_rate", "mean_throughput", "stdev_throughput", "median_time"]
        )
        for row in summary:
            writer.writerow(
                [
                    row["candidate"],
                    row["trials"],
                    row["hit_rate"],
                    row["mean_throughput"],
                    row["stdev_throughput"],
                    row["median_time"],
                ]
            )


def new_session(base: float, dpi: float, yaw: float, mode: str) -> dict:
    seed = random.SystemRandom().randint(1, 2_147_483_647)
    state = {
        "version": 2,
        "base": base,
        "dpi": dpi,
        "yaw": yaw,
        "mode": mode,
        "seed": seed,
        "search_rounds": SEARCH_ROUNDS[mode],
        "rounds": [],
        "candidates": [],
        "blocks": [],
        "block_index": 0,
        "warmup_done": False,
        "results": [],
        "active_results": [],
        "completed": False,
    }
    append_search_round(state, base, base * INITIAL_SEARCH_STEP_FACTOR)
    return state


def new_warmup_session(base: float, dpi: float, yaw: float) -> dict:
    return {
        "version": 2,
        "base": base,
        "dpi": dpi,
        "yaw": yaw,
        "mode": "warmup",
        "practice": True,
        "seed": random.SystemRandom().randint(1, 2_147_483_647),
        "results": [],
        "active_results": [],
        "completed": False,
    }


class Benchmark:
    def __init__(self, state: dict, store: SessionStore | None):
        self.state = state
        self.store = store
        self.screen = None
        self.clock = None
        self.font = None
        self.running = True
        self.current_trials: list[Trial] = []
        self.trial_index = 0
        self.current_trial: Trial | None = None
        self.current_block: dict | None = None
        self.trial_started_at: float | None = None
        self.feedback_started_at: float | None = None
        self.feedback_until: float = 0.0
        self.phase = "ready"
        self.yaw_position = 0.0
        self.pitch_position = 0.0
        self.last_result: TrialResult | None = None

    def _save(self) -> None:
        if self.store is not None:
            self.store.save(self.state)

    def run(self) -> dict:
        pygame.init()
        self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        self.clock = pygame.time.Clock()
        font_path = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "msyh.ttc"
        self.font = pygame.font.Font(str(font_path) if font_path.exists() else None, 24)
        pygame.event.set_grab(True)
        pygame.mouse.set_visible(False)
        self._prepare_next_block()
        while self.running:
            self._handle_events()
            self._update()
            self._draw()
            pygame.display.flip()
            self.clock.tick(144)
        pygame.event.set_grab(False)
        pygame.mouse.set_visible(True)
        pygame.quit()
        return self.state

    def _screen_aspect_ratio(self) -> float:
        width, height = self.screen.get_size()
        return width / height

    def _prepare_next_block(self) -> None:
        completed_trials = len(self.state["active_results"])
        if self.state.get("practice"):
            self.current_trials = make_trials(PRACTICE_TRIALS, self.state["seed"], self._screen_aspect_ratio())
            self.current_candidate = self.state["base"]
            self.current_warmup = False
            self.trial_index = completed_trials
            self.phase = "ready"
            return
        if not self.state["warmup_done"]:
            self.current_trials = make_trials(WARMUP_TRIALS, self.state["seed"], self._screen_aspect_ratio())
            self.current_candidate = self.state["base"]
            self.current_warmup = True
            self.trial_index = completed_trials
            self.phase = "ready"
            return
        if self.state["block_index"] >= len(self.state["blocks"]):
            if advance_search(self.state):
                self._save()
            else:
                self._save()
                self.running = False
                return
        block = self.state["blocks"][self.state["block_index"]]
        self.current_block = block
        block_seed = self.state["seed"] + self.state["block_index"] + 1
        self.current_trials = make_trials(
            TRIALS_PER_BLOCK[self.state["mode"]], block_seed, self._screen_aspect_ratio()
        )
        self.current_candidate = block["candidate"]
        self.current_warmup = False
        self.trial_index = completed_trials
        self.phase = "ready"

    def _start_trial(self) -> None:
        if self.trial_index >= len(self.current_trials):
            self._finish_block()
            return
        self.current_trial = self.current_trials[self.trial_index]
        self.yaw_position = 0.0
        self.pitch_position = 0.0
        self.trial_started_at = time.perf_counter()
        self.phase = "aiming"

    def _record_trial(self, hit: bool, elapsed: float) -> None:
        clamped = min(elapsed, TRIAL_TIMEOUT_SECONDS)
        result = TrialResult(
            candidate=self.current_candidate,
            distance=self.current_trial.distance,
            size=self.current_trial.size,
            hit=hit,
            elapsed=clamped,
            throughput=fitts_throughput(self.current_trial.distance, self.current_trial.size, clamped, hit),
            warmup=self.current_warmup,
        )
        self.last_result = result
        self.state["active_results"].append(asdict(result))
        self._save()
        self.trial_index += 1
        self.phase = "feedback"
        self.feedback_started_at = time.perf_counter()
        self.feedback_until = time.perf_counter() + 0.28
        play_feedback_sound(hit)

    def _finish_block(self) -> None:
        if self.state.get("practice"):
            self.state["results"].extend(self.state["active_results"])
            self.state["active_results"] = []
            self.state["completed"] = True
            self.running = False
            return
        if self.current_warmup:
            self.state["warmup_done"] = True
        else:
            self.state["results"].extend(self.state["active_results"])
            self.state["block_index"] += 1
        self.state["active_results"] = []
        self._save()
        self._prepare_next_block()

    def _handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    self.running = False
                elif event.key == pygame.K_SPACE and self.phase == "ready":
                    self._start_trial()
            elif event.type == pygame.MOUSEMOTION and self.phase == "aiming":
                multiplier = self.current_candidate * self.state["yaw"]
                self.yaw_position += event.rel[0] * multiplier
                self.pitch_position += event.rel[1] * multiplier
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and self.phase == "aiming":
                elapsed = time.perf_counter() - self.trial_started_at
                hit = self._is_on_target()
                self._record_trial(hit, elapsed)

    def _update(self) -> None:
        if self.phase == "aiming":
            if time.perf_counter() - self.trial_started_at >= TRIAL_TIMEOUT_SECONDS:
                self._record_trial(False, TRIAL_TIMEOUT_SECONDS)
        elif self.phase == "feedback" and time.perf_counter() >= self.feedback_until:
            self._start_trial()

    def _is_on_target(self) -> bool:
        horizontal_error = self.current_trial.horizontal - self.yaw_position
        vertical_error = self.current_trial.vertical - self.pitch_position
        return math.hypot(horizontal_error, vertical_error) <= self.current_trial.size / 2.0

    def _draw(self) -> None:
        width, height = self.screen.get_size()
        center = (width // 2, height // 2)
        half_w = width / 2.0
        self.screen.fill((0, 0, 0))
        crosshair = center
        if self.phase == "aiming":
            target = angle_to_screen(self.current_trial.horizontal, self.current_trial.vertical, width, height)
            crosshair = angle_to_screen(self.yaw_position, self.pitch_position, width, height)
            radius = max(4, int(math.tan(math.radians(self.current_trial.size / 2.0)) * half_w))
            target_fill = pygame.Surface((radius * 2, radius * 2), pygame.SRCALPHA)
            pygame.draw.circle(target_fill, (0, 220, 90, 45), (radius, radius), radius)
            self.screen.blit(target_fill, (target[0] - radius, target[1] - radius))
            pygame.draw.circle(self.screen, (0, 220, 90), target, radius, 2)
        pygame.draw.line(
            self.screen,
            (220, 220, 220),
            (crosshair[0] - 12, crosshair[1]),
            (crosshair[0] + 12, crosshair[1]),
            1,
        )
        pygame.draw.line(
            self.screen,
            (220, 220, 220),
            (crosshair[0], crosshair[1] - 12),
            (crosshair[0], crosshair[1] + 12),
            1,
        )
        label = "热身" if self.current_warmup else "正式测试"
        progress = f"{self.trial_index + (1 if self.phase == 'aiming' else 0)}/{len(self.current_trials)}"
        action = "空格键开始本组" if self.phase == "ready" else "Esc 保存并返回"
        message = f"{label}  {progress}    {action}"
        self.screen.blit(self.font.render(message, True, (230, 230, 230)), (24, 24))
        if self.phase == "aiming" and self.trial_started_at is not None:
            remaining = max(0.0, TRIAL_TIMEOUT_SECONDS - (time.perf_counter() - self.trial_started_at))
            pygame.draw.rect(self.screen, (70, 70, 70), (24, 56, 180, 5))
            pygame.draw.rect(self.screen, (0, 220, 90), (24, 56, int(180 * remaining / TRIAL_TIMEOUT_SECONDS), 5))
        if self.phase == "ready":
            instruction = "按空格键开始本组，之后目标会自动连续出现。"
            self.screen.blit(self.font.render(instruction, True, (230, 230, 230)), (24, height - 48))
        elif self.phase == "feedback":
            color = (0, 220, 90) if self.last_result.hit else (230, 70, 70)
            text = "命中" if self.last_result.hit else "未命中"
            progress = min(1.0, (time.perf_counter() - self.feedback_started_at) / 0.28)
            target = angle_to_screen(self.current_trial.horizontal, self.current_trial.vertical, width, height)
            if self.last_result.hit:
                ring_radius = int(20 + progress * 70)
                ring = pygame.Surface((ring_radius * 2, ring_radius * 2), pygame.SRCALPHA)
                pygame.draw.circle(ring, (*color, int(190 * (1.0 - progress))), (ring_radius, ring_radius), ring_radius, 3)
                self.screen.blit(ring, (target[0] - ring_radius, target[1] - ring_radius))
            else:
                target_radius = max(4, int(math.tan(math.radians(self.current_trial.size / 2.0)) * half_w))
                pygame.draw.circle(self.screen, color, target, target_radius, 2)
                pygame.draw.line(self.screen, color, (crosshair[0] - 13, crosshair[1] - 13), (crosshair[0] + 13, crosshair[1] + 13), 3)
                pygame.draw.line(self.screen, color, (crosshair[0] + 13, crosshair[1] - 13), (crosshair[0] - 13, crosshair[1] + 13), 3)
            self.screen.blit(self.font.render(text, True, color), (center[0] - 30, center[1] - 60))


class TutorialWindow:
    def __init__(self, parent: tk.Misc):
        self.window = tk.Toplevel(parent)
        self.window.title("怎么测试")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.grab_set()
        configure_theme(self.window)
        self._build()
        center_window(self.window, 520)
        self.window.bind("<Escape>", lambda _event: self.window.destroy())

    def _build(self) -> None:
        outer = ttk.Frame(self.window, style="App.TFrame", padding=(28, 24))
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)
        ttk.Label(outer, text="三步测出适合你的灵敏度", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 16))
        content = ttk.Frame(outer, style="Surface.TFrame", padding=(22, 18))
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(1, weight=1)
        for row, line in enumerate(TUTORIAL_LINES, start=1):
            ttk.Label(content, text=f"{row}.", style="Surface.TLabel").grid(row=row, column=0, sticky="nw", pady=8)
            ttk.Label(content, text=line, style="Surface.TLabel", wraplength=390, justify="left").grid(
                row=row, column=1, sticky="w", padx=(8, 0), pady=8
            )
        ttk.Label(outer, text="按 Esc 可以随时保存并返回。", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(14, 16))
        ttk.Button(outer, text="知道了", style="Primary.TButton", command=self.window.destroy).grid(row=3, column=0, sticky="e")


class SetupWindow:
    def __init__(self, store: SessionStore):
        self.store = store
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.resizable(False, False)
        configure_theme(self.root)
        try:
            self.existing = self.store.load()
            self.load_error = False
        except (OSError, json.JSONDecodeError):
            self.existing = None
            self.load_error = True
        defaults = self.existing or {}
        self.sensitivity = tk.StringVar(value=f"{defaults.get('base', 1.0):.4f}")
        self.dpi = tk.StringVar(value=f"{defaults.get('dpi', 800):g}")
        self.mode = tk.StringVar(value=defaults.get("mode", "reliable"))
        self.acceleration_status = mouse_acceleration_status()
        self.acceleration_frame: ttk.Frame | None = None
        self.status_title = tk.StringVar()
        self.status_detail = tk.StringVar()
        self.status_detail_label: ttk.Label | None = None
        self._build()
        self._update_status()
        self.root.bind("<Return>", lambda _event: self.primary_command())
        self.root.bind("<F1>", lambda _event: TutorialWindow(self.root))
        center_window(self.root, 660)
        self.first_entry.focus_set()

    def _build(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(30, 26))
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)

        header = ttk.Frame(outer, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        accent = tk.Frame(header, background=ACCENT, width=5, height=54)
        accent.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(0, 14))
        accent.grid_propagate(False)
        ttk.Label(header, text="测出适合你的灵敏度", style="Title.TLabel").grid(row=0, column=1, sticky="sw")
        ttk.Label(header, text="输入当前设置，然后开始测试", style="Subtitle.TLabel").grid(row=1, column=1, sticky="nw", pady=(2, 0))
        ttk.Button(header, text="怎么测？", style="Help.TButton", command=lambda: TutorialWindow(self.root)).grid(
            row=0, column=2, rowspan=2, sticky="e"
        )
        ttk.Separator(outer).grid(row=1, column=0, sticky="ew", pady=(20, 20))

        form = ttk.Frame(outer, style="Surface.TFrame", padding=(22, 18))
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        fields = (("当前 CS2 灵敏度", self.sensitivity), ("鼠标 DPI", self.dpi))
        for row, (label, variable) in enumerate(fields, start=1):
            ttk.Label(form, text=label, style="Surface.TLabel").grid(row=row, column=0, sticky="w", pady=8)
            entry = ttk.Entry(form, textvariable=variable, width=18)
            entry.grid(row=row, column=1, sticky="ew", padx=(22, 0), pady=8)
            if row == 1:
                self.first_entry = entry
        ttk.Label(form, text="测试精度", style="Surface.TLabel").grid(row=3, column=0, sticky="w", pady=8)
        mode_frame = ttk.Frame(form, style="Surface.TFrame")
        mode_frame.grid(row=3, column=1, sticky="w", padx=(22, 0), pady=8)
        mode_options = (
            ("quick", "快速（约 4 分钟）"),
            ("reliable", "精确（约 8 分钟，推荐）"),
        )
        for value, label in mode_options:
            ttk.Radiobutton(
                mode_frame, text=label, value=value, variable=self.mode, style="Surface.TRadiobutton"
            ).pack(side="left", padx=(0, 18))
        self.mode_detail = tk.StringVar()
        ttk.Label(form, textvariable=self.mode_detail, style="SurfaceMuted.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        self.mode.trace_add("write", lambda *_args: self._update_mode_detail())
        self._update_mode_detail()

        if self.acceleration_status == "enabled":
            self._build_acceleration_warning(outer)

        if self.existing or self.load_error:
            session = ttk.Frame(outer, style="Surface.TFrame", padding=(18, 14))
            session.grid(row=4, column=0, sticky="ew", pady=(14, 0))
            session.columnconfigure(0, weight=1)
            ttk.Label(session, textvariable=self.status_title, style="SurfaceTitle.TLabel").grid(row=0, column=0, sticky="w")
            self.status_detail_label = ttk.Label(
                session,
                textvariable=self.status_detail,
                style="SurfaceMuted.TLabel",
                wraplength=540,
                justify="left",
            )
            self.status_detail_label.grid(row=1, column=0, sticky="w", pady=(6, 0))

        ttk.Separator(outer).grid(row=5, column=0, sticky="ew", pady=(20, 16))
        actions = ttk.Frame(outer, style="App.TFrame")
        actions.grid(row=6, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)

        resumable = bool(self.existing and not self.existing.get("completed") and self.existing.get("version") == 2)
        completed = bool(self.existing and self.existing.get("completed"))
        self.primary_command = self._start_new
        col = 1
        if resumable:
            ttk.Button(actions, text="继续测试", style="Secondary.TButton", command=self._resume).grid(
                row=0, column=col, padx=(8, 0)
            )
            col += 1
        elif completed:
            ttk.Button(actions, text="查看上次结果", style="Secondary.TButton", command=self._show_existing_result).grid(
                row=0, column=col, padx=(8, 0)
            )
            col += 1
        ttk.Button(actions, text="开始热身", style="Secondary.TButton", command=self._start_warmup).grid(
            row=0, column=col, padx=(8, 0)
        )
        col += 1
        ttk.Button(actions, text="开始测算", style="Primary.TButton", command=self._start_new).grid(
            row=0, column=col, padx=(8, 0)
        )

    def _update_mode_detail(self) -> None:
        self.mode_detail.set(mode_description(self.mode.get()))

    def _build_acceleration_warning(self, outer: ttk.Frame) -> None:
        frame = ttk.Frame(outer, style="Surface.TFrame", padding=(18, 14))
        frame.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        frame.columnconfigure(0, weight=1)
        self.acceleration_frame = frame
        ttk.Label(frame, text="检测到 Windows 鼠标指针加速", style="Warning.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text="“增强指针精确度”会导致测量结果与游戏内手感不一致。请在 设置 → 鼠标 → 其他鼠标选项 中关闭，然后重新检测。",
            style="Warning.TLabel",
            wraplength=500,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(frame, text="重新检测", style="Help.TButton", command=self._recheck_acceleration).grid(
            row=2, column=0, sticky="e", pady=(10, 0)
        )

    def _recheck_acceleration(self) -> None:
        self.acceleration_status = mouse_acceleration_status()
        if self.acceleration_status == "enabled":
            messagebox.showwarning(APP_NAME, "指针加速仍然开启，请关闭后再次检测。", parent=self.root)
            return
        if self.acceleration_frame is not None:
            self.acceleration_frame.destroy()
        messagebox.showinfo(APP_NAME, "指针加速已关闭，测量结果会与游戏内一致。", parent=self.root)

    def _update_status(self) -> None:
        state = self.existing
        if self.load_error:
            self.status_title.set("记录文件无法读取")
            self.status_detail.set("可以开始新测试并替换损坏的记录。")
            if self.status_detail_label:
                self.status_detail_label.configure(style="Warning.TLabel")
            return
        if not state:
            self.status_title.set("准备就绪")
            self.status_detail.set("只需要这两项，其他设置使用 CS2 默认值。")
            return
        if not state.get("completed") and state.get("version") != 2:
            self.status_title.set("旧版进度无法继续")
            self.status_detail.set("开始新测试后会替换这份记录。")
            if self.status_detail_label:
                self.status_detail_label.configure(style="Warning.TLabel")
            return
        if not state.get("completed"):
            completed, total = session_progress(state)
            self.status_title.set("发现未完成测试")
            self.status_detail.set(f"已完成 {completed}/{total} 次（{completed / total:.0%}）")
            return
        self.status_title.set("上次测试已完成")
        try:
            summary = summarize_results(state_to_results(state["results"]), state["candidates"])
            selected = select_recommendation(summary)["selected"]
            self.status_detail.set(f"上次推荐灵敏度：{selected:.4f}")
        except (KeyError, TypeError, ValueError):
            self.status_detail.set("可以查看上次保存的结果。")

    def _start_new(self) -> None:
        try:
            state = new_session(
                validate_positive_number(self.sensitivity.get(), "sensitivity"),
                validate_positive_number(self.dpi.get(), "DPI"),
                0.022,
                self.mode.get(),
            )
        except ValueError as error:
            messagebox.showerror(APP_NAME, str(error), parent=self.root)
            return
        try:
            existing = self.store.load()
        except (OSError, json.JSONDecodeError):
            existing = None
        if existing:
            subject = "未完成进度" if not existing.get("completed") else "上次测试结果"
            if not messagebox.askyesno(APP_NAME, f"开始新测试会覆盖{subject}，是否继续？", parent=self.root):
                return
        self.store.save(state)
        self.root.destroy()
        Benchmark(state, self.store).run()
        self._after_benchmark()

    def _resume(self) -> None:
        state = self.store.load()
        if not state or state.get("version") != 2:
            return
        self.root.destroy()
        Benchmark(state, self.store).run()
        self._after_benchmark()

    def _start_warmup(self) -> None:
        try:
            state = new_warmup_session(
                validate_positive_number(self.sensitivity.get(), "sensitivity"),
                validate_positive_number(self.dpi.get(), "DPI"),
                0.022,
            )
        except ValueError as error:
            messagebox.showerror(APP_NAME, str(error), parent=self.root)
            return
        self.root.destroy()
        state = Benchmark(state, None).run()
        WarmupResultWindow(state, self.store).run()

    def _after_benchmark(self) -> None:
        state = self.store.load()
        if state and state.get("completed"):
            try:
                append_history(HISTORY_PATH, state)
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                pass
            ResultWindow(state, self.store).run()
        else:
            SetupWindow(self.store).run()

    def _show_existing_result(self) -> None:
        state = self.store.load()
        if not state or not state.get("completed"):
            messagebox.showinfo(APP_NAME, "没有已完成的测试。", parent=self.root)
            return
        self.root.destroy()
        ResultWindow(state, self.store).run()

    def run(self) -> None:
        self.root.mainloop()


class WarmupResultWindow:
    def __init__(self, state: dict, store: SessionStore):
        self.state = state
        self.store = store
        self.root = tk.Tk()
        self.root.title("CS2 热身结果")
        self.root.resizable(False, False)
        configure_theme(self.root)
        self.summary = summarize_results(state_to_results(state["results"]), [state["base"]])[0]
        self._build()
        center_window(self.root, 520)
        self.root.bind("<Escape>", lambda _event: self.root.destroy())

    def _build(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(30, 26))
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)
        ttk.Label(outer, text="热身完成", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text=f"固定灵敏度 {self.state['base']:.4f}，本轮不会影响测算结果。", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(4, 14)
        )
        result = ttk.Frame(outer, style="Surface.TFrame", padding=(20, 18))
        result.grid(row=2, column=0, sticky="ew")
        values = (
            ("命中率", f"{self.summary['hit_rate']:.0%}"),
            ("中位用时", f"{self.summary['median_time']:.2f}s" if self.summary["median_time"] is not None else "--"),
            ("试次数", str(self.summary["trials"])),
        )
        for column, (label, value) in enumerate(values):
            result.columnconfigure(column, weight=1)
            ttk.Label(result, text=label, style="SurfaceMuted.TLabel").grid(row=0, column=column)
            ttk.Label(result, text=value, style="SurfaceTitle.TLabel", font=("Segoe UI Semibold", 18)).grid(
                row=1, column=column, pady=(5, 0)
            )
        ttk.Button(outer, text="返回首页", style="Primary.TButton", command=self._home).grid(
            row=3, column=0, sticky="ew", pady=(18, 0)
        )

    def _home(self) -> None:
        self.root.destroy()
        SetupWindow(self.store).run()

    def run(self) -> None:
        self.root.mainloop()


class ResultWindow:
    def __init__(self, state: dict, store: SessionStore):
        self.state = state
        self.store = store
        self.root = tk.Tk()
        self.root.title("CS2 测试结果")
        self.root.resizable(False, False)
        configure_theme(self.root)
        self.summary = summarize_results(state_to_results(state["results"]), state["candidates"])
        self.recommendation = select_recommendation(self.summary)
        self.history = load_history(HISTORY_PATH)
        self._build()
        center_window(self.root, 720)
        self.root.bind("<Escape>", lambda _event: self.root.destroy())

    def _build(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(30, 26))
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)
        selected = self.recommendation["selected"]
        command = f"sensitivity {selected:.4f}"
        selected_row = next(row for row in self.summary if row["candidate"] == selected)
        median_time = selected_row["median_time"]
        cm360 = cm_per_360(self.state["dpi"], selected, self.state["yaw"])
        edpi = selected * self.state["dpi"]
        mode_label = "精确" if self.state.get("mode") == "reliable" else "快速"

        ttk.Label(outer, text="适合你的 CS2 灵敏度", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text="把游戏里的灵敏度设置成这个值", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(4, 14)
        )
        result = ttk.Frame(outer, style="Surface.TFrame", padding=(24, 20))
        result.grid(row=2, column=0, sticky="ew")
        result.columnconfigure(0, weight=1)
        ttk.Label(result, text=f"{selected:.4f}", style="Result.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(result, text=command, style="SurfaceMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))
        low, high = self.recommendation["range"]
        range_text = (
            "本次只有一个表现最佳的候选值。"
            if low == high
            else f"{low:.4f} 到 {high:.4f} 的表现接近，优先从 {selected:.4f} 开始。"
        )
        ttk.Label(result, text=range_text, style="SurfaceMuted.TLabel").grid(row=2, column=0, sticky="w", pady=(6, 0))

        metrics = ttk.Frame(outer, style="Surface.TFrame", padding=(18, 14))
        metrics.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        metric_values = (
            ("命中率", f"{selected_row['hit_rate']:.0%}"),
            ("中位用时", f"{median_time:.2f}s" if median_time is not None else "--"),
            ("平均吞吐", f"{selected_row['mean_throughput']:.2f}"),
            ("CM/360°", f"{cm360:.1f}"),
            ("eDPI", f"{edpi:.0f}"),
        )
        for column, (label, value) in enumerate(metric_values):
            metrics.columnconfigure(column, weight=1)
            cell = ttk.Frame(metrics, style="Surface.TFrame")
            cell.grid(row=0, column=column, sticky="ew", padx=(0, 8))
            cell.columnconfigure(0, weight=1)
            ttk.Label(cell, text=label, style="SurfaceMuted.TLabel").grid(row=0, column=0)
            ttk.Label(cell, text=value, style="SurfaceTitle.TLabel", font=("Segoe UI Semibold", 15)).grid(
                row=1, column=0, pady=(4, 0)
            )

        detail = ttk.Frame(outer, style="Surface.TFrame", padding=(18, 14))
        detail.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        detail.columnconfigure(0, weight=1)
        ttk.Label(detail, text=f"候选灵敏度明细（{mode_label}模式，共 {len(self.state.get('results', []))} 次有效试次）", style="SurfaceTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        columns = ("candidate", "trials", "hit_rate", "median_time", "mean_throughput")
        tree_frame = ttk.Frame(detail)
        tree_frame.grid(row=1, column=0, sticky="ew")
        tree_frame.columnconfigure(0, weight=1)
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=5)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        headings = (
            ("candidate", "灵敏度", 90),
            ("trials", "试次", 60),
            ("hit_rate", "命中率", 70),
            ("median_time", "中位用时", 90),
            ("mean_throughput", "平均吞吐", 90),
        )
        for column, title, width in headings:
            tree.heading(column, text=title)
            tree.column(column, width=width, anchor="center")
        tree.tag_configure("recommended", background="#e3f2eb")
        for row in self.summary:
            if not row["trials"]:
                continue
            tree.insert(
                "",
                "end",
                values=(
                    f"{row['candidate']:.4f}",
                    row["trials"],
                    f"{row['hit_rate']:.0%}",
                    f"{row['median_time']:.2f}s" if row["median_time"] is not None else "--",
                    f"{row['mean_throughput']:.2f}",
                ),
                tags=("recommended",) if row["candidate"] == selected else (),
            )
        tree.grid(row=0, column=0, sticky="ew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        history = [entry for entry in self.history if entry.get("recommended") is not None]
        if len(history) >= 2:
            history_frame = ttk.Frame(outer, style="Surface.TFrame", padding=(18, 14))
            history_frame.grid(row=5, column=0, sticky="ew", pady=(14, 0))
            history_frame.columnconfigure(0, weight=1)
            ttk.Label(history_frame, text="最近测试记录", style="SurfaceTitle.TLabel").grid(
                row=0, column=0, sticky="w", pady=(0, 8)
            )
            for index, entry in enumerate(reversed(history[-5:])):
                mode = "精确" if entry.get("mode") == "reliable" else "快速"
                ttk.Label(
                    history_frame,
                    text=f"{entry['timestamp']}  {mode}  →  推荐 {entry['recommended']:.4f}",
                    style="Surface.TLabel",
                ).grid(row=index + 1, column=0, sticky="w", pady=2)

        self.copy_button = ttk.Button(
            outer,
            text="复制 CS2 命令",
            style="Primary.TButton",
            command=lambda: self._copy(command),
        )
        self.copy_button.grid(row=6, column=0, sticky="ew", pady=(18, 10))
        actions = ttk.Frame(outer, style="App.TFrame")
        actions.grid(row=7, column=0, sticky="ew")
        actions.columnconfigure((0, 1, 2), weight=1)
        ttk.Button(actions, text="导出 CSV", style="Secondary.TButton", command=self._export_csv).grid(
            row=0, column=0, sticky="ew", padx=(0, 5)
        )
        ttk.Button(actions, text="返回首页", style="Secondary.TButton", command=self._home).grid(
            row=0, column=1, sticky="ew", padx=(5, 5)
        )
        ttk.Button(actions, text="关闭", style="Secondary.TButton", command=self.root.destroy).grid(
            row=0, column=2, sticky="ew", padx=(5, 0)
        )

    def _copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self.copy_button.configure(text="已复制")
        self.root.after(1200, lambda: self.copy_button.configure(text="复制 CS2 命令"))

    def _export_csv(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root,
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv")],
            initialfile="sensitivity_results.csv",
        )
        if not path:
            return
        try:
            export_results_csv(Path(path), self.state)
        except OSError as error:
            messagebox.showerror(APP_NAME, f"导出失败：{error}", parent=self.root)
            return
        messagebox.showinfo(APP_NAME, f"已导出到：\n{path}", parent=self.root)

    def _home(self) -> None:
        self.root.destroy()
        SetupWindow(self.store).run()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    enable_dpi_awareness()
    SetupWindow(SessionStore()).run()


if __name__ == "__main__":
    main()
