# SPDX-License-Identifier: GPL-3.0-or-later
"""Integration tests: run in a separate Blender with --factory-startup."""

import importlib.util
from pathlib import Path

import bmesh
import bpy
from mathutils import Euler, Matrix, Vector


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("selection_to_pivot", ROOT / "__init__.py")
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
addon.register()


def close(actual, expected):
    assert (actual - expected).length < 2e-4, (tuple(actual), tuple(expected))


def begin_group(groups, face_mode=False):
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    objects = []
    for index, points in enumerate(groups):
        world = Matrix.LocRotScale(
            Vector((index * 5 + 1, -2, 3)),
            Euler((0.2 * (index + 1), -0.4, 0.7)).to_quaternion(),
            Vector((1, 1, 1)) if index == 0 else Vector((1 + index, 0.8, 1.5)))
        mesh = bpy.data.meshes.new("Multi Edit Test")
        # Include unrelated geometry on each mesh to detect accidental inclusion.
        positions = [*points, Vector((100, 200, 300))]
        mesh.from_pydata([world.inverted() @ p for p in positions], [],
                         [tuple(range(len(points)))] if face_mode and points else [])
        obj = bpy.data.objects.new("Multi Edit Test", mesh)
        bpy.context.collection.objects.link(obj)
        obj.matrix_world = world
        obj.select_set(True)
        objects.append(obj)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.context.view_layer.update()
    bpy.context.scene.tool_settings.mesh_select_mode = (not face_mode, False, face_mode)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    for obj, points in zip(objects, groups):
        bm = bmesh.from_edit_mesh(obj.data)
        if face_mode:
            for face in bm.faces:
                face.select_set(True)
        else:
            for vertex in bm.verts:
                vertex.select_set(vertex.index < len(points))
        bmesh.update_edit_mesh(obj.data)
    return objects


def snapshot(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    return (obj.matrix_world.copy(), [obj.matrix_world @ v.co for v in bm.verts],
            [v.select for v in bm.verts], [v.co.copy() for v in bm.verts])


count = 0
center = Vector((4, -3, 6))
line = Vector((1, 2, 3)).normalized()
z = Vector((0, 0, 1))
cases = (
    # Active mesh can have no selected vertices: aim using the other mesh.
    (([], [center]), "point", center, False),
    (([center - line * 2], [center + line * 2]), line, center, False),
    (([center + Vector((1, 0, 0))], [center - Vector((1, 0, 0))],
      [center + Vector((0, 1, 0)), center - Vector((0, 1, 0))]), "symmetric", center, False),
    # Planar face selected on the non-active mesh, with non-uniform scale.
    (([], [center + Vector(offset) for offset in
           ((-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0))]), z, center, True),
)

try:
    for groups, direction, expected_center, face_mode in cases:
        for move_origin in (False, True):
            for axis in "XYZ":
                for negative in (False, True):
                    objects = begin_group(groups, face_mode)
                    active = objects[0]
                    before = [snapshot(obj) for obj in objects]
                    points, _, _ = addon.selected_points(bpy.context)
                    assert len(points) == sum(map(len, groups))
                    expected_direction = direction
                    if direction in ("point", "symmetric"):
                        expected_direction = z if move_origin else (
                            expected_center - active.matrix_world.translation).normalized()
                    assert bpy.ops.object.selection_to_pivot(
                        axis=axis, negative=negative, origin=move_origin) == {'FINISHED'}
                    assert bpy.context.object == active
                    assert set(bpy.context.objects_in_mode) == set(objects)
                    close(active.matrix_world.translation,
                          expected_center if move_origin else before[0][0].translation)
                    close(active.matrix_world.to_3x3().col["XYZ".index(axis)].normalized(),
                          -expected_direction if negative else expected_direction)
                    for index, obj in enumerate(objects):
                        after = snapshot(obj)
                        assert after[2] == before[index][2], "Selection changed"
                        for actual, expected in zip(after[1], before[index][1]):
                            close(actual, expected)
                        if index:
                            assert after[0] == before[index][0], "Non-active pivot changed"
                            assert after[3] == before[index][3], "Non-active mesh changed"
                    count += 1

    # Blender leaves linked copies in Object Mode. Selection still controls
    # compensation: selected copies keep geometry in place, unselected do not.
    for compensate in (False, True):
        for move_origin in (False, True):
            for axis in "XYZ":
                objects = begin_group(([center], [center + Vector((4, 0, 0))]))
                bpy.ops.object.mode_set(mode='OBJECT')
                active, other = objects
                other.data = active.data
                # Uniform scales retain the previous world-space invariance.
                other.matrix_world = active.matrix_world.copy()
                other.matrix_world.translation += Vector((4, 0, 0))
                other.select_set(compensate)
                bpy.context.view_layer.update()
                bpy.ops.object.mode_set(mode='EDIT')
                before_other = other.matrix_world.copy()
                before_active_origin = active.matrix_world.translation.copy()
                edited_objects = set(bpy.context.objects_in_mode)
                bm = bmesh.from_edit_mesh(active.data)
                before_geometry = [other.matrix_world @ v.co for v in bm.verts]
                points, _, _ = addon.selected_points(bpy.context)
                assert len(points) == 1
                expected_origin = points[0] if move_origin else before_active_origin
                assert bpy.ops.object.selection_to_pivot(
                    axis=axis, origin=move_origin) == {'FINISHED'}
                close(active.matrix_world.translation, expected_origin)
                bm = bmesh.from_edit_mesh(active.data)
                after_geometry = [other.matrix_world @ v.co for v in bm.verts]
                if compensate:
                    for actual, expected in zip(after_geometry, before_geometry):
                        close(actual, expected)
                else:
                    assert other.matrix_world == before_other
                    assert any((a - b).length > 1e-3
                               for a, b in zip(after_geometry, before_geometry))
                assert active.data == other.data
                assert other.select_get() == compensate
                assert set(bpy.context.objects_in_mode) == edited_objects
                count += 1

    print(f"PASS: {count} multi-edit checks (active pivot only, world selections, modes, faces, linked mesh)")
finally:
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    addon.unregister()
