"""Long-running simulation for interrupt testing."""

from yade import O
from yade.wrapper import *
from yade import utils

O.reset()
O.materials.append(FrictMat(young=1e6, poisson=0.3, density=2600, frictionAngle=0.5))

for i in range(20):
    O.bodies.append(utils.sphere((i * 0.05, 0, 0), radius=0.01))

O.dt = 1e-6
print("Starting long simulation: 10,000,000 iterations")

# Run in chunks to allow print updates
for chunk in range(1000):
    O.run(10000, True)
    print("Progress: {} / 10000000 iterations".format(O.iter))

print("Simulation completed: {} iterations".format(O.iter))
result = {"iterations": O.iter, "bodies": len(O.bodies)}
