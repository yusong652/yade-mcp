"""Simple YADE simulation test script for bridge execution."""

from yade import O
from yade.wrapper import *
from yade import utils

# Create a simple scene with a few spheres
O.materials.append(FrictMat(young=1e6, poisson=0.3, density=2600, frictionAngle=0.5))

for i in range(5):
    O.bodies.append(utils.sphere((i * 0.1, 0, 0), radius=0.02))

print("Created {} bodies".format(len(O.bodies)))
print("Materials: {}".format(len(O.materials)))

# Run a few steps
O.dt = 1e-6
O.run(100, True)  # 100 steps, wait=True

print("Simulation completed: {} iterations".format(O.iter))

result = {
    "bodies": len(O.bodies),
    "iterations": O.iter,
    "dt": O.dt,
}
