"""Arrange spheres to spell 'yade-mcp' for header visual."""

from yade import O, utils
from yade.wrapper import *

O.reset()

mat = FrictMat(young=1e7, poisson=0.3, density=2650, frictionAngle=0.5)
O.materials.append(mat)

# 5x7 lowercase pixel font (baseline at row 5, descenders go to row 6-7)
FONT = {
    'y': [
        ".....",
        ".....",
        "1...1",
        "1...1",
        ".1.1.",
        "..1..",
        ".1...",
    ],
    'a': [
        ".....",
        ".....",
        ".111.",
        "....1",
        ".1111",
        "1...1",
        ".1111",
    ],
    'd': [
        "....1",
        "....1",
        ".1111",
        "1...1",
        "1...1",
        "1...1",
        ".1111",
    ],
    'e': [
        ".....",
        ".....",
        ".111.",
        "1...1",
        "11111",
        "1....",
        ".111.",
    ],
    '-': [
        ".....",
        ".....",
        ".....",
        ".....",
        ".111.",
        ".....",
        ".....",
    ],
    'm': [
        ".....",
        ".....",
        "11.1.",
        "1.1.1",
        "1.1.1",
        "1...1",
        "1...1",
    ],
    'c': [
        ".....",
        ".....",
        ".111.",
        "1....",
        "1....",
        "1....",
        ".111.",
    ],
    'p': [
        ".....",
        ".....",
        "1111.",
        "1...1",
        "1111.",
        "1....",
        "1....",
    ],
}

text = "yade-mcp"
sphere_r = 0.016
spacing = sphere_r * 2.2  # tighter packing
letter_gap = sphere_r * 2.5  # tighter letter gap

x_offset = 0.0
for char in text:
    glyph = FONT[char]
    for row_idx, row in enumerate(glyph):
        for col_idx, pixel in enumerate(row):
            if pixel == '1':
                x = x_offset + col_idx * spacing
                y = 0
                z = (6 - row_idx) * spacing
                O.bodies.append(utils.sphere((x, y, z), radius=sphere_r))
    x_offset += len(glyph[0]) * spacing + letter_gap

# Minimal engines (no simulation, just display)
O.engines = [
    ForceResetter(),
    InsertionSortCollider([Bo1_Sphere_Aabb()]),
    InteractionLoop([], [], []),
    NewtonIntegrator(gravity=(0, 0, 0)),
]

# Export preview with matplotlib
data = []
for b in O.bodies:
    if hasattr(b.shape, 'radius'):
        pos = b.state.pos
        data.append((float(pos[0]), float(pos[2]), float(b.shape.radius)))

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(16, 4))
for x, z, r in data:
    circle = plt.Circle((x, z), r, fill=True, color='steelblue', alpha=0.85)
    ax.add_patch(circle)
ax.set_xlim(-0.05, x_offset + 0.05)
ax.set_ylim(-0.05, 7 * spacing + 0.05)
ax.set_aspect('equal')
ax.set_facecolor('#2d2d2d')
fig.patch.set_facecolor('#2d2d2d')
ax.axis('off')
plt.tight_layout()
plt.savefig('/tmp/yade_letters_v2.png', dpi=150, facecolor='#2d2d2d')
print("Created {} spheres for '{}'".format(len(data), text))
