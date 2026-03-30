"""Gravity deposition simulation - spheres falling into a box."""

from yade import O, utils
from yade.wrapper import *

O.reset()

# Material
mat = FrictMat(young=1e7, poisson=0.3, density=2650, frictionAngle=0.5)
O.materials.append(mat)

# Create box walls
ground = utils.wall((0, 0, 0), axis=2)  # z=0 ground
O.bodies.append(ground)

# Generate random spheres above the ground
import random
random.seed(42)
for i in range(200):
    x = random.uniform(-0.1, 0.1)
    y = random.uniform(-0.1, 0.1)
    z = random.uniform(0.05, 0.5)
    r = random.uniform(0.005, 0.015)
    O.bodies.append(utils.sphere((x, y, z), radius=r))

print("Created {} bodies (1 wall + {} spheres)".format(len(O.bodies), len(O.bodies) - 1))

# Engines
O.engines = [
    ForceResetter(),
    InsertionSortCollider([Bo1_Sphere_Aabb(), Bo1_Wall_Aabb()]),
    InteractionLoop(
        [Ig2_Sphere_Sphere_ScGeom(), Ig2_Wall_Sphere_ScGeom()],
        [Ip2_FrictMat_FrictMat_FrictPhys()],
        [Law2_ScGeom_FrictPhys_CundallStrack()],
    ),
    NewtonIntegrator(gravity=(0, 0, -9.81), damping=0.3),
]

O.dt = 0.5 * utils.PWaveTimeStep()
print("Time step: {:.2e}".format(O.dt))

# Run simulation in chunks
total_steps = 500000
chunk_size = 50000

print("Starting gravity deposition: {} steps".format(total_steps))
for i in range(total_steps // chunk_size):
    O.run(chunk_size, True)
    uf = utils.unbalancedForce()
    print("Step {}/{} | unbalanced force: {:.4f}".format(
        O.iter, total_steps, uf))
    if uf < 0.01:
        print("Equilibrium reached!")
        break

print("Simulation finished: {} iterations".format(O.iter))

result = {
    "bodies": len(O.bodies),
    "iterations": O.iter,
    "dt": O.dt,
    "final_unbalanced_force": utils.unbalancedForce(),
}
