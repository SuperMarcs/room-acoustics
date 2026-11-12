"""Exact rectilinear boundary mesh for two rooms, an enclosed corridor and doors.

Air volumes are boxes. Coordinate compression, rather than a fixed voxel size,
preserves doorway dimensions exactly. Adjacent air cells share no acoustic wall.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import math

import numpy as np
from scipy.ndimage import label


SPACES = ("room_a", "room_b", "corridor")
SPACE_NAMES = {"room_a": "Room A", "room_b": "Room B", "corridor": "Corridor"}
PLAN_FIELDS = ("room_a_width_m", "room_a_length_m", "room_b_width_m", "room_b_length_m",
               "corridor_depth_m", "height_m", "wall_thickness_m")


def space_boxes(plan):
    a, b, c, h, t = (plan[k] for k in ("room_a_width_m", "room_b_width_m", "corridor_depth_m",
                                      "height_m", "wall_thickness_m"))
    return {
        "room_a": ((0.0, c + t, 0.0), (a, c + t + plan["room_a_length_m"], h)),
        "room_b": ((a + t, c + t, 0.0), (a + t + b, c + t + plan["room_b_length_m"], h)),
        "corridor": ((0.0, 0.0, 0.0), (a + t + b, c, h)),
    }


def dimensions(plan):
    boxes = space_boxes(plan)
    return np.max([upper for _, upper in boxes.values()], axis=0).tolist()


def locate_space(plan, position):
    for key, (lower, upper) in space_boxes(plan).items():
        if all(lo < v < hi for lo, v, hi in zip(lower, position, upper)):
            return key
    return None


def door_bounds(plan, key):
    door = plan["doors"][key]
    base = space_boxes(plan)[key][0][0]
    return base + door["offset_m"], base + door["offset_m"] + door["width_m"]


def air_boxes(plan):
    boxes = list(space_boxes(plan).values())
    depth, thick = plan["corridor_depth_m"], plan["wall_thickness_m"]
    for key in ("room_a", "room_b"):
        door = plan["doors"][key]
        left, right = door_bounds(plan, key)
        height = door["height_m"]
        if door["open"]:
            boxes.append(((left, depth, 0.0), (right, depth + thick, height)))
        else:
            # A finite solid leaf separates two half-depth doorway reveals.
            leaf = min(0.04, thick / 3)
            middle = depth + thick / 2
            boxes.extend([((left, depth, 0.0), (right, middle - leaf / 2, height)),
                          ((left, middle + leaf / 2, 0.0), (right, depth + thick, height))])
    return boxes


def resize_positions(config, new_plan):
    """Keep each object's local position in its original room when dimensions change."""
    result = copy.deepcopy(config)
    old_boxes, new_boxes = space_boxes(config["floor_plan"]), space_boxes(new_plan)
    for collection in ("speakers", "listeners"):
        for item in result[collection]:
            key = locate_space(config["floor_plan"], item["position_m"])
            if key is None:
                raise ValueError("Every object must be inside Room A, Room B, or the corridor")
            lower, upper = new_boxes[key]
            local = np.subtract(item["position_m"], old_boxes[key][0])
            size = np.subtract(upper, lower)
            margin = np.minimum(0.01, size / 10)
            item["position_m"] = (np.asarray(lower) + np.clip(local, margin, size - margin)).tolist()
    result["floor_plan"] = copy.deepcopy(new_plan)
    return result


def validate_plan(plan):
    for key in PLAN_FIELDS:
        value = plan[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be a finite positive number")
    if plan["wall_thickness_m"] < 0.05:
        raise ValueError("Wall thickness must be at least 0.05 m")
    for key in ("room_a", "room_b"):
        door = plan["doors"][key]
        for field in ("offset_m", "width_m", "height_m"):
            value = door[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} door {field} must be positive")
        if not isinstance(door["open"], bool):
            raise ValueError(f"{key} door open must be true or false")
        if door["offset_m"] + door["width_m"] >= plan[f"{key}_width_m"]:
            raise ValueError(f"{SPACE_NAMES[key]} door must fit strictly inside its wall")
        if door["height_m"] > plan["height_m"]:
            raise ValueError(f"{SPACE_NAMES[key]} door cannot be taller than the ceiling")


@dataclass
class Surface:
    corners: np.ndarray
    kind: str


def rectangles(mask):
    """Merge coplanar cells into nonoverlapping rectangles with no holes."""
    remaining = mask.copy()
    while remaining.any():
        i, j = np.argwhere(remaining)[0]
        end_j = j + 1
        while end_j < remaining.shape[1] and remaining[i, end_j]:
            end_j += 1
        end_i = i + 1
        while end_i < remaining.shape[0] and remaining[end_i, j:end_j].all():
            end_i += 1
        remaining[i:end_i, j:end_j] = False
        yield i, end_i, j, end_j


class Geometry:
    def __init__(self, plan):
        self.plan = plan
        boxes = air_boxes(plan)
        self.axes = [np.unique([point[axis] for box in boxes for point in box]) for axis in range(3)]
        centers = [(axis[:-1] + axis[1:]) / 2 for axis in self.axes]
        self.air = np.zeros(tuple(len(axis) - 1 for axis in self.axes), dtype=bool)
        for lower, upper in boxes:
            indices = [np.flatnonzero((center > lo) & (center < hi))
                       for center, lo, hi in zip(centers, lower, upper)]
            self.air[np.ix_(*indices)] = True
        self.components, self.count = label(self.air)

    def component_at(self, position):
        indices = tuple(int(np.searchsorted(axis, point, side="right") - 1)
                        for axis, point in zip(self.axes, position))
        if any(i < 0 or i >= size for i, size in zip(indices, self.air.shape)):
            return 0
        return int(self.components[indices])

    def volume(self, component):
        cell_volumes = np.einsum("i,j,k->ijk", *(np.diff(axis) for axis in self.axes))
        return float(cell_volumes[self.components == component].sum())

    def surfaces(self, component):
        occupied = self.components == component
        result = []
        for axis in range(3):
            u, v = [index for index in range(3) if index != axis]
            moved = np.moveaxis(occupied, axis, 0)
            padded = np.pad(moved, ((1, 1), (0, 0), (0, 0)))
            for plane in range(len(self.axes[axis])):
                before, after = padded[plane], padded[plane + 1]
                for sign, mask in ((1, before & ~after), (-1, after & ~before)):
                    # Split closed door faces from the surrounding wall so their
                    # absorption can be specified separately.
                    tags = np.zeros(mask.shape, dtype=int)
                    if axis == 1:
                        for key in ("room_a", "room_b"):
                            door = self.plan["doors"][key]
                            if door["open"]:
                                continue
                            leaf = min(0.04, self.plan["wall_thickness_m"] / 3)
                            middle = self.plan["corridor_depth_m"] + self.plan["wall_thickness_m"] / 2
                            if abs(abs(self.axes[axis][plane] - middle) - leaf / 2) < 1e-8:
                                left, right = door_bounds(self.plan, key)
                                uc = (self.axes[u][:-1] + self.axes[u][1:]) / 2
                                vc = (self.axes[v][:-1] + self.axes[v][1:]) / 2
                                tags[np.ix_((uc > left) & (uc < right), vc < door["height_m"])] = 1
                    for tag in (0, 1):
                        for i0, i1, j0, j1 in rectangles(mask & (tags == tag)):
                            corners = np.zeros((3, 4))
                            corners[axis] = self.axes[axis][plane]
                            corners[u] = self.axes[u][[i0, i1, i1, i0]]
                            corners[v] = self.axes[v][[j0, j0, j1, j1]]
                            normal = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
                            if normal[axis] * sign < 0:
                                corners = corners[:, ::-1].copy()
                            kind = "door" if tag else ("ceiling" if sign > 0 else "floor") if axis == 2 else "wall"
                            result.append(Surface(corners, kind))
        return result


def plan_segments(plan):
    """Top-view wall outlines at ear height, plus colored door-state segments."""
    geom = Geometry(plan)
    ear_height = min(1.2, plan["height_m"] / 2)
    walls = []
    for component in range(1, geom.count + 1):
        for surface in geom.surfaces(component):
            corners = surface.corners
            if np.ptp(corners[2]) == 0 or not corners[2].min() <= ear_height < corners[2].max():
                continue
            xy = np.unique(corners[:2].T, axis=0)
            walls.append(xy)
    doors = []
    y = plan["corridor_depth_m"] + plan["wall_thickness_m"] / 2
    for key in ("room_a", "room_b"):
        left, right = door_bounds(plan, key)
        doors.append((key, np.array([[left, y], [right, y]]), plan["doors"][key]["open"]))
    return walls, doors
