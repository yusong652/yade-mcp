"""Large gravity deposition simulation - 2000 spheres for interrupt testing."""

from yade import O, utils
from yade.wrapper import *

O.reset()

mat = FrictMat(young=1e7, poisson=0.3, density=2650, frictionAngle=0.5)
O.materials.append(mat)

# Box walls
O.bodies.append(utils.wall((0, 0, 0), axis=2))      # ground z=0
O.bodies.append(utils.wall((-0.3, 0, 0), axis=0))    # left
O.bodies.append(utils.wall((0.3, 0, 0), axis=0))     # right
O.bodies.append(utils.wall((0, -0.3, 0), axis=1))    # front
O.bodies.append(utils.wall((0, 0.3, 0), axis=1))     # back

import random
random.seed(42)
for i in range(2000):
    x = random.uniform(-0.25, 0.25)
    y = random.uniform(-0.25, 0.25)
    z = random.uniform(0.05, 2.0)
    r = random.uniform(0.005, 0.015)
    O.bodies.append(utils.sphere((x, y, z), radius=r))

print("Created {} bodies (5 walls + 2000 spheres)".format(len(O.bodies)))

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

total_steps = 5000000
chunk_size = 50000

print("Starting large gravity deposition: {} steps".format(total_steps))
for i in range(total_steps // chunk_size):
    O.run(chunk_size, True)
    uf = utils.unbalancedForce()
    print("Step {}/{} | unbalanced force: {:.4f} | contacts: {}".format(
        O.iter, total_steps, uf, len([i for i in O.interactions if i.isReal])))
    if uf < 0.005:
        print("Equilibrium reached!")
        break

print("Simulation finished: {} iterations".format(O.iter))
result = {
    "bodies": len(O.bodies),
    "iterations": O.iter,
    "final_unbalanced_force": utils.unbalancedForce(),
}
