# Drained triaxial — Stage 1: sample prep + isotropic consolidation to 100 kPa
#
# Convention: YADE TriaxialStressController uses continuum-mechanics signs
# (positive = tension), so compressive target stress is NEGATIVE here.
# In console output we flip the sign back to "compression positive" kPa for
# readability.
from yade import pack
from math import radians, pi

# ---------- box and material ----------
# 3000 spheres, r in [0.5, 2.0] mm. Generous box for the loose cloud; walls
# will move inward to reach the target stress.
mn = Vector3(0, 0, 0)
mx = Vector3(0.05, 0.05, 0.05)  # 50 mm cube — loose enough for makeCloud

young = 1e8           # Pa
poisson = 0.3
density = 2600.0      # kg/m^3
compFricDeg = 1.0     # low friction during compaction → denser packing
finalFricDeg = 30.0   # restored before shear (next stage)

O.materials.append(
    FrictMat(
        young=young,
        poisson=poisson,
        frictionAngle=radians(compFricDeg),
        density=density,
        label="spheres",
    )
)

# ---------- walls + sphere cloud ----------
walls = aabbWalls([mn, mx], thickness=0, material="spheres")
wallIds = O.bodies.append(walls)

sp = pack.SpherePack()
# psdSizes/psdCumm define the grain-size distribution.
# A linear (uniform-by-number) PSD between 0.5 mm and 2 mm:
n_placed = sp.makeCloud(
    mn,
    mx,
    psdSizes=[0.5e-3, 2.0e-3],
    psdCumm=[0.0, 1.0],
    num=3000,
)
sp.toSimulation(material="spheres")

n_spheres = sum(1 for b in O.bodies if isinstance(b.shape, Sphere))
print(f"[prep] requested 3000, placed {n_placed}, in scene {n_spheres}")
print(f"[prep] box: {(mx-mn)[0]*1e3:.1f} mm cube  V_box={(mx-mn)[0]**3*1e6:.2f} cm^3")

# ---------- engines ----------
triax = TriaxialStressController(
    thickness=0,
    stressMask=7,                  # all three axes are stress-controlled
    internalCompaction=False,      # external compaction: walls move
    goal1=-100e3, goal2=-100e3, goal3=-100e3,  # 100 kPa compression
    max_vel=1.0,
    finalMaxMultiplier=1.00001,
    maxMultiplier=1.005,
    label="triax",
)

newton = NewtonIntegrator(damping=0.4)

O.engines = [
    ForceResetter(),
    InsertionSortCollider([Bo1_Sphere_Aabb(), Bo1_Box_Aabb()]),
    InteractionLoop(
        [Ig2_Sphere_Sphere_ScGeom(), Ig2_Box_Sphere_ScGeom()],
        [Ip2_FrictMat_FrictMat_FrictPhys()],
        [Law2_ScGeom_FrictPhys_CundallStrack()],
    ),
    GlobalStiffnessTimeStepper(
        active=1,
        timeStepUpdateInterval=100,
        timestepSafetyCoefficient=0.8,
        label="dtStepper",
    ),
    triax,
    newton,
    PyRunner(command="checkConsolidation()", iterPeriod=200, label="checker"),
]
O.dt = 1e-6

target_kPa = 100.0
stress_tol_kPa = 1.0     # within 1 kPa of target
unb_tol = 0.01           # quasi-static threshold


def checkConsolidation():
    s_kPa = -triax.meanStress / 1e3   # flip sign: compression positive
    unb = unbalancedForce()
    n = triax.porosity
    e = n / (1.0 - n) if n < 1.0 else float("inf")
    print(
        f"iter={O.iter:>6d}  s_mean={s_kPa:7.2f} kPa  "
        f"unb={unb:.3e}  n={n:.4f}  e={e:.4f}"
    )
    if O.iter > 500 and abs(s_kPa - target_kPa) < stress_tol_kPa and unb < unb_tol:
        print(
            f"=== CONVERGED at iter={O.iter}: s_mean={s_kPa:.2f} kPa, "
            f"unb={unb:.3e}, e0={e:.4f}, n0={n:.4f} ==="
        )
        O.pause()


print("[run] starting isotropic consolidation to 100 kPa ...")
O.run(400000, wait=True)

# ---------- final report + save ----------
s_final = -triax.meanStress / 1e3
n_final = triax.porosity
e_final = n_final / (1 - n_final)
print(
    f"[done] iter={O.iter}  s_mean={s_final:.2f} kPa  "
    f"n={n_final:.4f}  e={e_final:.4f}  "
    f"box=({triax.width:.4f},{triax.height:.4f},{triax.depth:.4f}) m"
)

save_path = "triax_consolidated_100kPa.yade.bz2"
O.save(save_path)
print(f"[save] state written to {save_path}")
