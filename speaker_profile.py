"""Portable Speaker Studio complex-response bridge (no image/file-path coupling)."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.interpolate import CubicSpline

FORMAT = "speaker-studio-room-profile"
MAX_POINTS = 50000
MAX_FILE_BYTES = 12_000_000


def validate_profile(raw):
    if not isinstance(raw, dict) or raw.get("format") != FORMAT or raw.get("version") != 1:
        raise ValueError("Choose speaker_room_profile.json exported with Speaker Studio's Export to room button.")
    if (raw.get("reference_distance_m") != 1.0
            or raw.get("propagation_delay_removed") is not True
            or raw.get("equivalent_omnidirectional") is not True):
        raise ValueError("Profile must be the on-axis 1 m equivalent source with common propagation delay removed.")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 1000:
        raise ValueError("Speaker profile needs a nonempty name (at most 1000 characters).")
    arrays = []
    for key in ("frequency_hz", "pressure_real_pa", "pressure_imag_pa"):
        value = raw.get(key)
        if not isinstance(value, list) or not 2 <= len(value) <= MAX_POINTS:
            raise ValueError(f"Profile {key} must contain 2–{MAX_POINTS} numeric values.")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
            raise ValueError(f"Profile {key} must contain numbers, not strings or booleans.")
        array = np.asarray(value, dtype=float)
        if array.ndim != 1 or not np.all(np.isfinite(array)):
            raise ValueError(f"Profile {key} must contain finite values.")
        arrays.append(array)
    freq, real, imag = arrays
    if not len(freq) == len(real) == len(imag):
        raise ValueError("Profile arrays must have equal length.")
    if freq[0] <= 0 or freq[-1] > 100000 or np.any(np.diff(freq) <= 0):
        raise ValueError("Profile frequencies must be positive, strictly increasing, and at most 100 kHz.")
    for key in ("reference_voltage_rms", "sound_speed_m_s"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value <= 0:
            raise ValueError(f"Profile {key} must be a positive finite number.")
    # Copy plain JSON only: embedded profiles make saved room layouts portable.
    return json.loads(json.dumps(raw, allow_nan=False))


def load_profile(path):
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Speaker profile exceeds 12 MB; use Speaker Studio's compact room export.")
    return validate_profile(json.loads(path.read_text(encoding="utf-8")))


def profile_id(profile):
    data = json.dumps(profile, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def source_profile(cfg, index):
    identifier = cfg["speakers"][index].get("profile_id", "")
    return cfg.get("speaker_profiles", {}).get(identifier) if identifier else None


def attach_profile(cfg, index, profile):
    """Copy-on-write assignment. The same design is stored once for a stereo pair."""
    result = copy.deepcopy(cfg)
    if profile is None:
        result["speakers"][index].pop("profile_id", None)
    else:
        profile = validate_profile(profile)
        identifier = profile_id(profile)
        result.setdefault("speaker_profiles", {})[identifier] = profile
        result["speakers"][index]["profile_id"] = identifier
    used = {s.get("profile_id") for s in result["speakers"]}
    result["speaker_profiles"] = {k: v for k, v in result.get("speaker_profiles", {}).items() if k in used}
    return result


def snapshot_config(cfg):
    """Explicitly freeze embedded response arrays before starting a report worker."""
    return copy.deepcopy(cfg)


def validate_assignments(cfg):
    library = cfg.setdefault("speaker_profiles", {})
    if not isinstance(library, dict) or len(library) > 32:
        raise ValueError("speaker_profiles must be a dictionary with at most 32 designs.")
    used = set()
    for speaker in cfg["speakers"]:
        identifier = speaker.get("profile_id", "")
        if not isinstance(identifier, str):
            raise ValueError("Speaker profile_id must be a string.")
        if identifier:
            if identifier not in library:
                raise ValueError(f"{speaker['name']}: assigned speaker profile is missing from the configuration.")
            used.add(identifier)
    for identifier in used:
        profile = validate_profile(library[identifier])
        if profile_id(profile) != identifier:
            raise ValueError("An embedded speaker profile was changed without updating its ID; load the export again.")
        library[identifier] = profile
    cfg["speaker_profiles"] = {k: library[k] for k in sorted(used)}
    # The sum needs a shared band; never extrapolate unknown measured response.
    lo, hi = cfg["plots"]["frequency_range_hz"]
    for profile in cfg["speaker_profiles"].values():
        lo = max(lo, profile["frequency_hz"][0])
        hi = min(hi, profile["frequency_hz"][-1])
    if lo >= hi:
        raise ValueError("Assigned speaker designs must share some frequencies inside the report's frequency range.")
    return cfg


def profile_response(profile, frequencies):
    """Interpolate Cartesian complex pressure; unsupported frequencies remain NaN.

    Dense 1 Hz exported samples retain crossover notches and phase/delay. Cubic
    interpolation avoids phase-wrap interpolation and adds no transport delay.
    """
    frequencies = np.asarray(frequencies, dtype=float)
    if profile is None:
        return np.ones(frequencies.shape, dtype=complex) # flat 1 Pa at 1 m
    f = np.asarray(profile["frequency_hz"], dtype=float)
    pressure = np.asarray(profile["pressure_real_pa"]) + 1j * np.asarray(profile["pressure_imag_pa"])
    output = np.full(frequencies.shape, np.nan + 1j * np.nan)
    valid = (frequencies >= f[0]) & (frequencies <= f[-1])
    output[valid] = CubicSpline(f, pressure, extrapolate=False)(frequencies[valid])
    return output


def combine_responses(cfg, room_transfers, frequencies):
    """Apply each source design BEFORE coherently summing the room channels.

    PRA's point-source Green function is 1/(4πr). A profile in Pa at 1 m
    therefore multiplies it by 4π. Gain and source delay are already in room H.
    """
    sources = np.asarray([profile_response(source_profile(cfg, i), frequencies)
                          for i in range(len(cfg["speakers"]))])
    pressure = np.asarray(room_transfers)[:, :len(sources), :] * (4 * np.pi) * sources[None, :, :]
    if len(sources) > 1:
        pressure = np.concatenate([pressure, pressure.sum(axis=1, keepdims=True)], axis=1)
    return pressure, sources
