"""
Distributed-Ollama server pool for the tiny animation models.

Per model role ("face", "arms"), an ordered preference list of Ollama
servers -- each entry naming the server URL and the model tag it serves
(a beefy LAN box can run q8_0 while the robot's own CPU runs q4_K_M as
the last resort). The pool probes the list and hands out the best live
`(generate_url, model)` pair.

Probing never sits on the generation path (Mark's rule: polling is fine
as long as it never adds latency). One synchronous probe at startup picks
the initial server; after that a daemon thread re-probes on a slow period
and atomically swaps the cached choice -- so a dead server gets dropped
within ~a minute and the pool upgrades back automatically when a better
one reappears. Three consecutive generation failures trigger an immediate
out-of-band re-probe instead of waiting for the next tick.

Zero-config fallback: no config file (or no entry for the role) yields a
pinned single-server pool identical to the old hard-coded default, and a
`~model` ROS param override on the animator nodes skips the pool entirely.
The LUT cascade fallback lives above this layer; nothing here may block a
performance.
"""

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

DEFAULT_CONFIG_PATH = "/home/robot/robot_ws/config/ollama_servers.json"
DEFAULT_PROBE_TIMEOUT_S = 2.0
DEFAULT_PROBE_INTERVAL_S = 60.0
FAILURES_BEFORE_REPROBE = 3
# After a server racks up FAILURES_BEFORE_REPROBE generation errors it is
# demoted (skipped by probing) for this long, then becomes eligible again.
# Covers the nasty case of a box that answers /api/tags fine but 400s on
# /api/generate (e.g. an old Ollama with no structured-output support): the
# probe can't detect that, so we route around it reactively and let it heal.
DEMOTE_COOLDOWN_S = 120.0


def generate_url(base: str) -> str:
    """Server base URL -> /api/generate endpoint (idempotent)."""
    base = base.rstrip("/")
    if base.endswith("/api/generate"):
        return base
    return base + "/api/generate"


def _entry_key(entry: Dict[str, str]) -> str:
    """Stable identity for a server entry (for the demotion blacklist)."""
    return entry["url"] + "|" + entry["model"]


def _tags_url(base: str) -> str:
    base = base.rstrip("/")
    if base.endswith("/api/generate"):
        base = base[: -len("/api/generate")]
    return base + "/api/tags"


def _server_has_model(base: str, model: str, timeout_s: float) -> bool:
    """GET /api/tags within timeout and confirm the model tag is present."""
    try:
        resp = requests.get(_tags_url(base), timeout=timeout_s)
        resp.raise_for_status()
        names = [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:
        return False
    # Ollama resolves tags case-insensitively at generate time (q4_K_M ==
    # Q4_K_M), so the probe must compare the same way.
    wanted = model.lower()
    lowered = {n.lower() for n in names}
    if wanted in lowered:
        return True
    # An untagged model name matches any quant of itself.
    return ":" not in wanted and wanted in {n.split(":", 1)[0] for n in lowered}


class OllamaPool:
    """
    Holds the ordered server list for one role and the current choice.
    current() is a lock-protected cache read -- never a network call.
    """

    def __init__(
        self,
        role: str,
        entries: List[Dict[str, str]],
        probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
        probe_interval_s: float = DEFAULT_PROBE_INTERVAL_S,
        pinned: bool = False,
        log: Optional[Callable[[str], None]] = None,
    ):
        if not entries:
            raise ValueError("OllamaPool needs at least one server entry")
        self.role = role
        self.entries = entries
        self.probe_timeout_s = float(probe_timeout_s)
        self.probe_interval_s = float(probe_interval_s)
        self.pinned = pinned or len(entries) == 1
        self._log = log or (lambda msg: print("[ollama_pool] " + msg))
        self._lock = threading.Lock()
        self._reprobe_now = threading.Event()
        self._consecutive_failures = 0
        # entry key -> monotonic-ish expiry time; a demoted entry is skipped
        # by the probe until it expires (see DEMOTE_COOLDOWN_S).
        self._demoted: Dict[str, float] = {}
        # Last entry is the configured last resort: used verbatim when no
        # server answers the probe, so a cold Ollama that comes up a moment
        # later still gets traffic (and the cascade covers the meantime).
        self._current = entries[-1]

        if not self.pinned:
            self._current = self._probe() or entries[-1]
            self._log("{}: using {} ({})".format(
                role, self._current["url"], self._current["model"]))
            thread = threading.Thread(
                target=self._probe_loop, daemon=True,
                name="ollama-pool-" + role)
            thread.start()

    # ── Generation-path API (cache reads only) ───────────────────────

    def current(self) -> Tuple[str, str]:
        """(generate_url, model) for the best known live server."""
        with self._lock:
            entry = self._current
        return generate_url(entry["url"]), entry["model"]

    def report_success(self) -> None:
        self._consecutive_failures = 0

    def report_failure(self) -> None:
        """
        Count consecutive generation failures; at the threshold, demote the
        current server (so the probe routes around it even if it still
        answers /api/tags) and re-probe immediately for the next live one.
        """
        if self.pinned:
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= FAILURES_BEFORE_REPROBE:
            self._consecutive_failures = 0
            with self._lock:
                bad = self._current
                self._demoted[_entry_key(bad)] = time.time() + DEMOTE_COOLDOWN_S
            self._log("{}: demoting {} ({}) for {:.0f}s after repeated "
                      "generation failures".format(
                          self.role, bad["url"], bad["model"], DEMOTE_COOLDOWN_S))
            self._reprobe_now.set()

    # ── Probing (startup + background thread) ────────────────────────

    def _probe(self) -> Optional[Dict[str, str]]:
        now = time.time()
        with self._lock:
            demoted = {k: exp for k, exp in self._demoted.items() if exp > now}
            self._demoted = demoted
        # First pass honors demotions; if that finds nothing, fall back to a
        # pass that ignores them so a fully-demoted pool still tries servers
        # rather than freezing on the last-resort entry.
        for skip_demoted in (True, False):
            for entry in self.entries:
                if skip_demoted and _entry_key(entry) in demoted:
                    continue
                if _server_has_model(entry["url"], entry["model"], self.probe_timeout_s):
                    return entry
            if not demoted:
                break
        return None

    def _probe_loop(self) -> None:
        while True:
            self._reprobe_now.wait(timeout=self.probe_interval_s)
            self._reprobe_now.clear()
            best = self._probe()
            if best is None:
                continue  # keep the current choice; cascade covers failures
            with self._lock:
                changed = best is not self._current
                self._current = best
            if changed:
                self._log("{}: switching to {} ({})".format(
                    self.role, best["url"], best["model"]))


def pinned(url: str, model: str) -> OllamaPool:
    """Single-server pool that never probes (zero-config / ~model override)."""
    return OllamaPool("pinned", [{"url": url, "model": model}], pinned=True)


def load_pool(
    role: str,
    config_path: Optional[str],
    default_url: str,
    default_model: str,
    log: Optional[Callable[[str], None]] = None,
) -> OllamaPool:
    """
    Build the pool for a role from the JSON config; any missing/invalid
    config (or role absent from it) degrades to a pinned pool on the
    old single-server defaults so nothing breaks without the file.
    """
    config: Dict[str, Any] = {}
    if config_path and os.path.isfile(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            if log:
                log("ollama servers config {} unreadable ({}); using defaults".format(
                    config_path, exc))
    entries = [
        {"url": e["url"], "model": e["model"]}
        for e in config.get(role, [])
        if isinstance(e, dict) and e.get("url") and e.get("model")
    ]
    if not entries:
        return pinned(default_url, default_model)
    return OllamaPool(
        role,
        entries,
        probe_timeout_s=float(config.get("probe_timeout_s", DEFAULT_PROBE_TIMEOUT_S)),
        probe_interval_s=float(config.get("probe_interval_s", DEFAULT_PROBE_INTERVAL_S)),
        log=log,
    )
